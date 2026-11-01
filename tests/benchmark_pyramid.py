#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure many-file fan-in: base tiles merge first, shared low zoom builds once.

The synthetic files all supply the same z=1 key as well as one distinct z=14
tile. Supplied overviews are ignored. RSS includes only the CLI process tree;
fixture creation and validation run outside the measured interval.
"""
import argparse
from contextlib import closing
import io
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw, features
from benchmark_memory import tree_rss

ROOT = Path(__file__).resolve().parents[1]


def run(args, count):
    with tempfile.TemporaryDirectory(prefix="mbmerge-fanin-", dir=args.work_dir) as tmp:
        root = Path(tmp)
        inputs = root / "inputs"
        inputs.mkdir()
        output = root / "merged.mbtiles"
        width = 128  # keeps all <=16384 fixtures in the NE z=1 tile
        base, parents = set(), set()
        input_bytes = 0
        buf = io.BytesIO()
        with Image.new("RGBA", (args.tile_size, args.tile_size), (30, 90, 180, 255)) as im:
            ImageDraw.Draw(im).rectangle((0, 0, args.tile_size // 3, args.tile_size - 1), fill=(220, 50, 30, 160))
            im.save(buf, "PNG")
        blob = buf.getvalue()
        for i in range(count):
            key = (14, 8192 + i % width, 8192 + i // width)
            base.add(key)
            z, x, y = key
            while z:
                z, x, y = z - 1, x // 2, y // 2
                parents.add((z, x, y))
            path = inputs / f"source_{i:05}.mbtiles"
            # A sqlite3 connection context commits but does NOT close. Explicit
            # closure is required for our own offline-input contract, especially
            # when thousands of connections would otherwise wait for cyclic GC.
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB,PRIMARY KEY(zoom_level,tile_column,tile_row))")
                db.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO metadata VALUES('format','png')")
                db.executemany("INSERT INTO tiles VALUES(?,?,?,?)", [(*key, blob), (1, 1, 1, blob)])
                db.commit()
            input_bytes += path.stat().st_size
        cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "merge", "--input-dir", str(inputs),
               "--output", str(output), "--tile-size", str(args.tile_size), "--workers", str(args.workers),
               "--output-format", args.output_format, "--json-log"]
        start, peak, max_processes = time.monotonic(), 0, 0
        with (root / "run.log").open("w+") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=log)
            try:
                while proc.poll() is None:
                    rss, processes = tree_rss(proc.pid)
                    peak, max_processes = max(peak, rss), max(max_processes, processes)
                    time.sleep(.1)
                elapsed = time.monotonic() - start
                log.seek(0)
                text = log.read()
                if proc.returncode:
                    raise RuntimeError(text[-12000:])
                stats = json.loads(text.splitlines()[-1])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=60)
        with sqlite3.connect(output) as db:
            keys = set(db.execute("SELECT zoom_level,tile_column,tile_row FROM tiles"))
            if keys != base | parents:
                raise RuntimeError("Pyramid coordinates differ from independent ancestor set")
            if stats["overview_tiles"] != len(parents):
                raise RuntimeError("A parent was built more than once during the initial import")
            if db.execute("SELECT count(*) FROM _merge_dirty").fetchone()[0]:
                raise RuntimeError("Dirty keys remain after completion")
            if db.execute("SELECT sum(tile_count) FROM _merge_sources").fetchone()[0] != count:
                raise RuntimeError("Supplied low-zoom contributions were imported")
        check = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "verify", "--output", str(output),
                                "--decode", "--full-sqlite-check"], capture_output=True, text=True, timeout=300)
        if check.returncode:
            raise RuntimeError(check.stderr)
        result = {"sources": count, "tile_size": args.tile_size, "input_bytes": input_bytes,
                  "output_bytes": output.stat().st_size, "elapsed_seconds": round(elapsed, 3),
                  "peak_tree_rss_mib": round(peak / 1024**2, 2), "max_processes": max_processes,
                  "base_tiles": len(base), "overview_tiles": len(parents),
                  "supplied_z1_contributions": count, "generated_z1_tiles": sum(z == 1 for z, x, y in parents),
                  "pipeline": stats, "verify": json.loads(check.stdout)}
        print(json.dumps(result), file=sys.stderr, flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=int, nargs="+", default=[128, 7000])
    parser.add_argument("--tile-size", type=int, default=512)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-format", choices=("png", "webp"), default="webp")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if any(not 1 <= n <= 16384 for n in args.sources) or not 1 <= args.tile_size <= 4096 or args.workers < 1:
        parser.error("sources must be 1..16384; tile size 1..4096; workers >= 1")
    report = {"cli_version": __import__("merge_mbtiles").VERSION,
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "platform": platform.platform(), "webp": features.version("webp"),
              "output_format": args.output_format, "workers": args.workers,
              "measurement": "sampled CLI parent+descendant RSS; shared pages can be counted more than once; excludes fixture generation, verification and filesystem page cache",
              "runs": [run(args, count) for count in args.sources]}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    # Direct invocation sets sys.path to tests/, unlike unittest discovery.
    sys.path.insert(0, str(ROOT))
    main()
