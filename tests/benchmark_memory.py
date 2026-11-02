#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""POSIX process-tree RSS sampling; synthetic inputs, no real data required.
Run from the project directory: python tests/benchmark_memory.py --tiles 64 512
Temporary DBs are deleted, report is JSON on stdout. Shared pages are counted per process.
"""
import argparse
from contextlib import closing
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from PIL import Image, features

ROOT = Path(__file__).resolve().parents[1]


def tree_rss(root_pid):
    """Sum RSS for the CLI and all descendants, including the resource tracker.

    RSS is reported in KiB by ps. Shared pages may be counted in multiple
    processes; the benchmark harness and OS filesystem cache are excluded.
    """
    proc = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=True)
    rows = [tuple(map(int, line.split())) for line in proc.stdout.splitlines() if line.strip()]
    wanted = {root_pid}
    # Iterate to a fixed point: workers can themselves have helper descendants.
    while True:
        new = wanted | {pid for pid, parent, rss in rows if parent in wanted}
        if new == wanted:
            return sum(rss for pid, parent, rss in rows if pid in wanted) * 1024, len(wanted)
        wanted = new


def blobs(size):
    """Build deterministic noisy RGBA inputs, making real codec work measurable.

    Keep random RGB under transparent pixels too; exact WebP retains it. The
    seed keeps fixtures comparable without storing large generated databases.
    """
    rng = random.Random(20261031)
    images = []
    for half in (0, 1):
        raw = bytearray(rng.randbytes(size * size * 4))
        for i in range(size * size):
            x = i % size
            raw[4 * i + 3] = 140 if ((x < size * 3 // 4) if half == 0 else (x >= size // 4)) else 0
        im = Image.frombytes("RGBA", (size, size), bytes(raw))
        out = io.BytesIO()
        im.save(out, "WEBP", lossless=True, exact=True, method=4)
        images.append(out.getvalue())
    return images


def fixture(path, data, count):
    """Stream repeated encoded tiles to SQLite without retaining all rows."""
    z = max(1, math.ceil(math.log2(math.ceil(math.sqrt(count)))))
    width = 1 << z
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB,PRIMARY KEY(zoom_level,tile_column,tile_row))")
        db.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO metadata VALUES('format','webp')")
        db.executemany("INSERT INTO tiles VALUES(?,?,?,?)", ((z, i // width, i % width, data) for i in range(count)))
        db.commit()


def main(args):
    """Measure separate fresh imports; temporary inputs/outputs are discarded.

    Fixture creation is outside the timed run and outside the measured process
    tree. Timings include CLI startup, source hashing, import and finalization.
    """
    report = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "platform": platform.platform(), "webp": features.version("webp"),
              "workers": args.workers, "tile_size": args.tile_size, "output_format": args.output_format,
              "measurement": "sampled simultaneous sum of parent+descendant RSS; shared pages counted multiple times; excludes filesystem page cache", "runs": []}
    a_data, b_data = blobs(args.tile_size)
    for count in args.tiles:
        with tempfile.TemporaryDirectory(dir=args.work_dir) as tmp:
            root = Path(tmp)
            a, b, out = [root / n for n in ("a.mbtiles", "b.mbtiles", "out.mbtiles")]
            fixture(a, a_data, count)
            fixture(b, b_data, count)
            cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "merge", "--output", str(out),
                   "--input", str(a), "--input", str(b), "--tile-size", str(args.tile_size),
                   "--workers", str(args.workers), "--max-inflight", str(args.workers * 2), "--output-format", args.output_format, "--json-log"]
            start, peak, max_processes = time.monotonic(), 0, 0
            # File-backed logs cannot fill a pipe and stall the measured CLI.
            with (root / "run.log").open("w+") as logfile:
                proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile)
                try:
                    while proc.poll() is None:
                        rss, processes = tree_rss(proc.pid)
                        peak, max_processes = max(peak, rss), max(max_processes, processes)
                        time.sleep(.1)
                    if proc.returncode:
                        logfile.seek(0)
                        raise RuntimeError(logfile.read())
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=60)
                logfile.seek(0)
                final = json.loads(logfile.read().splitlines()[-1])
            run = {"tiles_per_source": count, "contributions": count * 2,
                   "input_bytes": a.stat().st_size + b.stat().st_size,
                   "output_bytes": out.stat().st_size, "elapsed_seconds": round(time.monotonic() - start, 3),
                   "peak_tree_rss_mib": round(peak / (1024 * 1024), 2), "max_processes": max_processes,
                   "pipeline": final}
            report["runs"].append(run)
            print(json.dumps(run), file=sys.stderr, flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tiles", type=int, nargs="+", default=[64, 512])
    p.add_argument("--tile-size", type=int, default=512)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output-format", choices=("webp", "png"), default="webp")
    p.add_argument("--work-dir", type=Path, help="directory for temporary DBs (about 1 GiB for default run)")
    args = p.parse_args()
    if any(n < 1 for n in args.tiles) or args.tile_size < 1 or args.workers < 1:
        p.error("positive counts/sizes are required")
    main(args)
