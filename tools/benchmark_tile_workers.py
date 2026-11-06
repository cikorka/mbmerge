#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Memory and speed of `mbmerge tile` workers in a fixed time window (Linux, reads /proc).

    tools/benchmark_tile_workers.py --seconds 300 -- tile --input-dir SRC --pattern '*.jpg' \
        --s-srs EPSG:5514 --output /tmp/probe.mbtiles --overwrite --zoom 19 --threads 4 --worker-cache-mib 128

Everything after `--` is passed to mbmerge; --json-log is added. The run is killed after
--seconds (or when it ends) and one JSON line is printed: peak RSS of the parent, of each
worker (max and median) and of the whole tree, and base tiles per second between the
--warmup second and the end of the window. Compare settings on the same input and window;
on a shared machine expect a few percent of noise.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def rss_mb(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def children(pid):
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                    out.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                pass
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=int, default=300)
    p.add_argument("--warmup", type=int, default=60, help="seconds excluded from the rate (default 60)")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("mbmerge", nargs=argparse.REMAINDER)
    args = p.parse_args()
    cmd = [a for a in args.mbmerge if a != "--"]
    if not cmd:
        p.error("give the mbmerge command after --")
    log = tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False)
    proc = subprocess.Popen([sys.executable, str(ROOT / "merge_mbtiles.py"), *cmd, "--json-log"],
                            stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
    workers, parent_peak, total_peak = {}, 0.0, 0.0
    start = time.monotonic()
    while time.monotonic() - start < args.seconds and proc.poll() is None:
        parent = rss_mb(proc.pid)
        total = parent
        for w in children(proc.pid):
            r = rss_mb(w)
            total += r
            workers[w] = max(workers.get(w, 0.0), r)
        parent_peak, total_peak = max(parent_peak, parent), max(total_peak, total)
        time.sleep(args.interval)
    finished = proc.poll() is not None
    if not finished:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    ticks = [json.loads(line) for line in Path(log.name).read_text().splitlines() if '"tiling"' in line]
    os.unlink(log.name)
    warm = [t for t in ticks if t["seconds"] >= args.warmup]
    rate = None
    if len(warm) >= 2:
        rate = round((warm[-1]["tiles"] - warm[0]["tiles"]) / (warm[-1]["seconds"] - warm[0]["seconds"]), 1)
    peaks = sorted(v for v in workers.values() if v > 50)  # worker processes, not short-lived GDAL tools
    print(json.dumps({
        "seconds": round(time.monotonic() - start), "finished": finished, "returncode": proc.returncode if finished else None,
        "worker_peak_max_mb": round(max(peaks)) if peaks else None,
        "worker_peak_median_mb": round(statistics.median(peaks)) if peaks else None,
        "parent_peak_mb": round(parent_peak), "total_peak_mb": round(total_peak),
        "base_tiles_per_s": rate, "rows_done": ticks[-1]["rows_done"] if ticks else 0,
    }))


if __name__ == "__main__":
    main()
