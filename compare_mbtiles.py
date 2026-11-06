# SPDX-License-Identifier: MIT
"""`mbmerge compare`: how close are tiles of one or more MBTiles to their source raster?

The reference for a tile is rendered straight from the sources (GDAL warp into the
tile's bounds in EPSG:3857, `--resampling`, no lossy step). Only tiles fully opaque in
the reference and in every candidate are scored, over the same random sample for all
candidates. Reports PSNR (mean and 5th percentile = the worst tiles) and SSIM per zoom,
with tile count and mean encoded size, as JSON.

Compare at equal size: a higher --quality always wins on PSNR.
"""
from __future__ import annotations

import io
import json
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

WORLD = 40075016.685578488
OPAQUE_SHARE = 0.95  # tiles less opaque than this are edges (collar, cutline), not imagery


def _tiles(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    size = db.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
    return db, size


def _psnr(a, b, mask, np):
    mse = ((a.astype(np.float64) - b.astype(np.float64)) ** 2)[mask].mean()
    return 99.0 if mse == 0 else float(10 * np.log10(255 ** 2 / mse))


def compare(args):
    import numpy as np
    from osgeo import gdal
    from PIL import Image
    from skimage.metrics import structural_similarity

    from tile_mbtiles import TileError, collect

    gdal.UseExceptions()
    files = collect(args)
    grader = None
    if args.grade:  # the candidates were graded: grade the reference the same way, so only encoding loss is scored
        from grade_mbtiles import Grader, load

        grader = Grader(load(args.grade))
    candidates = {}
    for spec in args.candidate:
        name, sep, path = spec.partition("=")
        if not sep:
            name, path = Path(spec).stem, spec
        candidates[name] = sqlite3.connect(f"file:{Path(path).expanduser().resolve(strict=True)}?mode=ro", uri=True)
    if not candidates:
        raise TileError("Give at least one --candidate NAME=PATH.mbtiles")
    with tempfile.TemporaryDirectory(prefix="mbmerge-compare-") as tmp:
        vrt = str(Path(tmp) / "src.vrt")
        gdal.BuildVRT(vrt, [str(f) for f in files], outputSRS=args.s_srs)
        rng = random.Random(args.seed)
        report = {}
        for z in args.zoom:
            keys = None
            for db in candidates.values():
                found = set(db.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,)))
                keys = found if keys is None else keys & found
            pool = sorted(keys)
            rng.shuffle(pool)
            scores = {name: [] for name in candidates}
            ts = None
            for x, y in pool:
                if len(next(iter(scores.values()))) >= args.samples:
                    break
                blobs = {n: db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                       (z, x, y)).fetchone()[0] for n, db in candidates.items()}
                tiles = {n: np.asarray(Image.open(io.BytesIO(b)).convert("RGBA")) for n, b in blobs.items()}
                ts = next(iter(tiles.values())).shape[0]
                size = WORLD / 2 ** z
                minx, miny = -WORLD / 2 + x * size, -WORLD / 2 + y * size  # TMS row: y counts from the south
                ref = gdal.Warp("", vrt, format="MEM", dstSRS="EPSG:3857", width=ts, height=ts,
                                outputBounds=(minx, miny, minx + size, miny + size), resampleAlg=args.resampling,
                                dstAlpha=True).ReadAsArray().transpose(1, 2, 0)
                if grader is not None:
                    ref = grader(np.ascontiguousarray(ref))
                mask = ref[..., 3] == 255
                for t in tiles.values():
                    mask &= t[..., 3] == 255
                if mask.mean() < OPAQUE_SHARE:
                    continue
                for n, t in tiles.items():
                    scores[n].append((_psnr(ref[..., :3], t[..., :3], mask, np),
                                      structural_similarity(ref[..., :3], t[..., :3], channel_axis=2, data_range=255)))
            for n, db in candidates.items():
                count, total = db.execute("SELECT count(*), coalesce(sum(length(tile_data)), 0) FROM tiles "
                                          "WHERE zoom_level = ?", (z,)).fetchone()
                s = np.array(scores[n]) if scores[n] else None
                report.setdefault(n, {})[str(z)] = {
                    "tiles": count, "kib_per_tile": round(total / 1024 / max(count, 1), 1),
                    "scored": 0 if s is None else len(s),
                    "psnr": None if s is None else round(float(s[:, 0].mean()), 2),
                    "psnr_p5": None if s is None else round(float(np.percentile(s[:, 0], 5)), 2),
                    "ssim": None if s is None else round(float(s[:, 1].mean()), 4),
                }
    json.dump(report, sys.stdout, indent=1)
    print()


def add_parser(sub):
    c = sub.add_parser("compare", help="PSNR/SSIM of MBTiles tiles against a direct render of their sources "
                                       "(needs numpy, GDAL bindings, scikit-image)")
    c.add_argument("inputs", nargs="*", help="source rasters")
    c.add_argument("--input-dir")
    c.add_argument("--pattern", default="*.tif", help="glob within --input-dir; ** supported")
    c.add_argument("--s-srs", metavar="SRS", help="coordinate system of sources without one (world files)")
    c.add_argument("--candidate", action="append", default=[], metavar="NAME=PATH",
                   help="MBTiles to score (repeatable); all are scored on the same tiles")
    c.add_argument("--zoom", type=int, action="append", required=True, metavar="Z", help="repeatable")
    c.add_argument("--samples", type=int, default=60, help="scored tiles per zoom (default 60)")
    c.add_argument("--resampling", default="average", help="of the reference render (default average)")
    c.add_argument("--seed", type=int, default=1)
    c.add_argument("--grade", metavar="GRADE_JSON", help="grade the reference like `tile --grade` did")
    c.set_defaults(func=compare, json_log=False)
    return c
