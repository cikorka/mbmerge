# SPDX-License-Identifier: MIT
"""Georeferenced rasters -> one flat MBTiles (lossless by default) that `mbmerge merge` accepts.

Pipeline (GDAL >= 3.11 command-line tools plus its Python bindings and numpy,
which must belong to the same libgdal):

1. per input: PDF -> GeoTIFF at --pdf-dpi; a source without a coordinate
   system gets --s-srs or is refused; palettes are expanded to RGBA;
   edge-connected collars -> alpha (`nearblack -setalpha`, --collar); exact
   colors -> alpha everywhere (`gdalwarp -srcnodata ... -dstalpha`, --nodata).
   Declared nodata, masks and alpha bands of the source are always honoured.
2. several inputs -> one mosaic (`gdalbuildvrt -addalpha`, later inputs on top).
3. one warp to EPSG:3857 at exactly the resolution of --zoom, as a VRT aligned
   to the tile grid and limited to the --cutline extent.
4. base tiles straight into the MBTiles: worker processes read rectangles of
   tiles from the warp row by row, burn the cutline into alpha in the target
   grid, drop fully transparent tiles, apply --binary-alpha and encode
   (lossless, or lossy WebP with --quality for imagery that is not merged).
   The parent is the only SQLite writer; nothing is staged on disk.
5. levels down to --min-zoom: 2x2 average with premultiplied alpha, the same
   method `merge` uses for its pyramid. The workers build the first
   PYRAMID_DEPTH levels from the pixels they have just encoded; lower levels
   come from the stored tiles above. With --quality those are lossless copies
   kept until the end, so no level is ever averaged from decoded lossy tiles.

Only fully transparent tiles count as empty. Colors are never interpreted while
tiling: turning black/white collars into transparency is step 1's job, before
any resampling can blend them into valid pixels.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ProcessPoolExecutor
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import tempfile
import time
import traceback

from merge_mbtiles import CHILD_OFFSETS, log, worker_init

WORLD = 40075016.685578488  # EPSG:3857 extent width in metres
MIN_GDAL = (3, 11)
RESAMPLING = ("auto", "nearest", "bilinear", "cubic", "lanczos", "average")
# Work is split into rectangles: a band of tile columns x contiguous tile rows.
# A worker reads its rectangle one row of tiles per GDAL request, top to
# bottom, so sources are read forward (strip TIFFs and JPEGs cannot seek
# backwards cheaply) and rows shared by neighbouring requests come from the
# block cache. Rasters up to READ_COLUMNS_MAX tiles wide (a scanned plan) are
# read in full-width rows, so a strip is never decompressed twice. Wider ones
# (mosaics of sheets) get bands of READ_COLUMNS_WIDE tiles: a request then
# touches only one or two sheets, whose rows the cache can keep.
READ_COLUMNS_MAX = 64  # 64 x 512 px RGBA = 64 MiB per request
READ_COLUMNS_WIDE = 16
JOBS_PER_WORKER = 4  # several rectangles per worker balance areas outside a cutline
MIN_JOBS_PER_WORKER = 2  # below this the in-memory pyramid depth is lowered (see write_base)
# Levels below the base built by the tile workers from pixels in memory (one
# pending row per level); lower ones from the stored tiles of the level above.
PYRAMID_DEPTH = 4
WORKER_CACHE_MIB = 256  # GDAL block cache per worker
QUALITY_RANGE = range(1, 101)
OVERVIEW_INFLIGHT = 8  # overview jobs per worker submitted ahead of the writer
OVERVIEW_COMMIT = 512  # overview tiles per transaction
PROGRESS_SECONDS = 30


class TileError(Exception):
    """Expected tiling failure (missing GDAL, bad input, failed command)."""


def run(cmd, args):
    """Run one GDAL tool; its stderr becomes the error message on failure."""
    log(args, "gdal", command=" ".join(map(str, cmd[:2])))
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if proc.returncode != 0:
        raise TileError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def gdal_version():
    """CLI version string; the Python bindings must be the same GDAL."""
    try:
        out = subprocess.run(["gdal", "--version"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise TileError("GDAL command-line tools not found; `mbmerge tile` needs GDAL >= 3.11 on PATH") from None
    match = re.search(r"GDAL (\d+\.\d+\.\d+)", out)
    if not match or tuple(map(int, match.group(1).split(".")[:2])) < MIN_GDAL:
        raise TileError(f"`mbmerge tile` needs GDAL >= 3.11 (found: {out.strip() or 'unknown'})")
    try:
        import numpy  # noqa: F401  (ReadAsArray)
        from osgeo import gdal
    except ImportError as e:
        raise TileError(f"`mbmerge tile` needs the GDAL Python bindings and numpy ({e})") from None
    if gdal.__version__ != match.group(1):
        raise TileError(f"GDAL Python bindings {gdal.__version__} differ from command-line tools {match.group(1)}")
    return out.strip()


def info(path, args):
    return json.loads(run(["gdalinfo", "-json", "-nomd", path], args))


def parse_color(value):
    """'R,G,B' (0..255 each) -> (r, g, b); also accepts the names black/white."""
    named = {"black": (0, 0, 0), "white": (255, 255, 255)}
    if value in named:
        return named[value]
    parts = value.split(",")
    if len(parts) != 3 or not all(p.strip().isdigit() and int(p) <= 255 for p in parts):
        raise TileError(f"Invalid color {value!r}; expected R,G,B with values 0-255, black or white")
    return tuple(int(p) for p in parts)


def collect(args):
    files = [Path(p).expanduser().resolve(strict=True) for p in args.inputs]
    if args.input_dir:
        root = Path(args.input_dir).expanduser().resolve(strict=True)
        files += sorted(p.resolve() for p in root.glob(args.pattern) if p.is_file())
    files = list(dict.fromkeys(files))
    if not files:
        raise TileError("No inputs found")
    return files


def prepare_input(index, path, work, args):
    """Make one input ready for mosaicking: rasterized, georeferenced, RGB(A), collars/colors -> alpha."""
    src = path
    if path.suffix.lower() == ".pdf":
        src = work / f"{index}.tif"
        run(["gdal_translate", "-q", "--config", "GDAL_PDF_DPI", str(args.pdf_dpi), "-of", "GTiff",
             "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", path, src], args)
    meta = info(src, args)
    if not meta.get("coordinateSystem", {}).get("wkt"):
        # A world file (.jgw, .tfw) carries the geotransform only; GDAL would
        # otherwise silently take the coordinates as EPSG:3857 metres.
        if not args.s_srs:
            raise TileError(f"{path.name} has no coordinate system (a world file alone does not define one); "
                            "pass --s-srs, e.g. --s-srs EPSG:5514")
        out = work / f"{index}_srs.vrt"
        run(["gdal_translate", "-q", "-of", "VRT", "-a_srs", args.s_srs, src, out], args)
        src = out
    if meta["bands"] and "colorTable" in meta["bands"][0]:
        out = work / f"{index}_rgba.vrt"
        run(["gdal_translate", "-q", "-of", "VRT", "-expand", "rgba", src, out], args)
        src = out
    if args.collar:
        out = work / f"{index}_collar.tif"
        cmd = ["nearblack", "-q", "-of", "GTiff", "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE",
               "-o", out, "-near", str(args.near), "-setalpha"]
        for color in args.collar:
            cmd += ["-color", ",".join(map(str, color))]
        run(cmd + [src], args)
        src = out
    for k, color in enumerate(args.nodata):
        out = work / f"{index}_nodata{k}.vrt"
        run(["gdalwarp", "-q", "-of", "VRT", "-srcnodata", " ".join(map(str, color)),
             "-wo", "UNIFIED_SRC_NODATA=YES", "-dstalpha", src, out], args)
        src = out
    return src


def native_resolution(src, work, args):
    """Pixel size GDAL itself proposes for the source in EPSG:3857."""
    probe = work / "probe.vrt"
    run(["gdalwarp", "-q", "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857", src, probe], args)
    gt = info(probe, args)["geoTransform"]
    return abs(gt[1])


def tile_zoom(resolution, tile_size):
    """Zoom whose resolution is nearest (in log scale) to the source's."""
    return max(0, min(30, round(math.log2(WORLD / (tile_size * resolution)))))


def cutline_extent(work, args):
    """Select the cutline features into a small EPSG:3857 layer; return (path, extent).

    Reprojecting the polygon once means the later rasterization needs no
    transformation, and its extent bounds the warp to the clipped area.
    """
    cut = work / "cutline.gpkg"
    cmd = ["ogr2ogr", "-q", "-f", "GPKG", "-t_srs", "EPSG:3857", "-nln", "cutline"]
    if args.cwhere:
        cmd += ["-where", args.cwhere]
    cmd += [cut, Path(args.cutline).expanduser().resolve(strict=True)]
    if args.cutline_layer:
        cmd.append(args.cutline_layer)
    run(cmd, args)
    out = run(["ogrinfo", "-so", cut, "cutline"], args)
    count = re.search(r"Feature Count: (\d+)", out)
    match = re.search(r"Extent: \(([-\d.e]+), ([-\d.e]+)\) - \(([-\d.e]+), ([-\d.e]+)\)", out)
    if not count or int(count.group(1)) == 0 or not match:
        raise TileError(f"Cutline selects no feature ({args.cwhere or 'all'})")
    return cut, tuple(float(v) for v in match.groups())


def source_extent(src, work, args):
    """Extent of the source in EPSG:3857, from an unaligned warp VRT (gdalwarp transforms the edges densely)."""
    probe = work / "extent.vrt"
    run(["gdalwarp", "-q", "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857", src, probe], args)
    meta = info(probe, args)
    gt, (width, height) = meta["geoTransform"], meta["size"]
    return gt[0], gt[3] + gt[5] * height, gt[0] + gt[1] * width, gt[3]


def intersect(a, b):
    minx, miny, maxx, maxy = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if minx >= maxx or miny >= maxy:
        raise TileError("The cutline does not overlap the input")
    return minx, miny, maxx, maxy


def warp(src, work, zoom, resampling, args):
    """Warp VRT in the tile grid of `zoom`; return (vrt, cutline layer or None).

    The warp stays a VRT that the tile workers read directly: gdalwarp writing
    to a file samples differently (measured: 8 % of pixels off by up to 82
    against an exact `-et 0` warp, whereas the VRT stays within 1).

    The cutline is not given to gdalwarp, which evaluates it in the pixel space
    of its *source*: neighbours clipped by one shared boundary would overlap or
    leave one-pixel cracks. The workers burn it in the target grid instead,
    where every pixel centre belongs to exactly one of them.
    """
    res = WORLD / (args.tile_size * 2 ** zoom)
    cmd = ["gdalwarp", "-q", "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857", "-tr", repr(res), repr(res),
           "-dstalpha", "-r", resampling]
    cut = None
    if args.cutline:
        cut, extent = cutline_extent(work, args)
        # only where the source has pixels: a country-wide cutline over one sheet
        # would otherwise make the workers walk the empty rows of the whole country
        minx, miny, maxx, maxy = intersect(extent, source_extent(src, work, args))
        # snap outward to the tile grid; equivalent to -tap, limited to the polygon
        cmd += ["-te", *(repr(v) for v in (math.floor(minx / res) * res, math.floor(miny / res) * res,
                                           math.ceil(maxx / res) * res, math.ceil(maxy / res) * res))]
    else:
        cmd += ["-tap"]
    aligned = work / "aligned.vrt"
    run(cmd + [src, aligned], args)
    return aligned, cut


def tile_layout(meta, zoom, tile_size):
    """Pixel offset of the warp in the zoom's world raster and its XYZ tile range."""
    gt = meta["geoTransform"]
    width, height = meta["size"]
    res = gt[1]
    px0 = round((gt[0] + WORLD / 2) / res)
    py0 = round((WORLD / 2 - gt[3]) / res)
    return {"zoom": zoom, "tile_size": tile_size, "res": res, "origin": (gt[0], gt[3]),
            "px0": px0, "py0": py0, "width": width, "height": height,
            "tx0": px0 // tile_size, "tx1": (px0 + width - 1) // tile_size,
            "ty0": py0 // tile_size, "ty1": (py0 + height - 1) // tile_size}


LOSSLESS_SOURCE = {"format": "webp", "quality": None, "webp_method": 0, "png_compress_level": 1}


def encode_tile(im, enc):
    """RGBA image -> tile bytes: lossless WebP/PNG, or lossy WebP when enc['quality'] is set."""
    out = io.BytesIO()
    if enc["format"] == "png":
        im.save(out, "PNG", compress_level=enc["png_compress_level"])
    elif enc["quality"] is None:
        im.save(out, "WEBP", lossless=True, exact=True, method=enc["webp_method"])
    else:
        im.save(out, "WEBP", quality=enc["quality"], method=enc["webp_method"])  # alpha stays lossless
    return out.getvalue()


def overview_tile(job, tile_size, enc):
    """One parent from its four children: premultiplied exact 2x2 average, as `merge` builds its pyramid."""
    from PIL import Image
    z, x, y, children = job
    with Image.new("RGBA", (tile_size * 2, tile_size * 2)) as canvas:
        for (dx, dy), data in zip(CHILD_OFFSETS, children):
            if data is not None:  # a missing quadrant stays transparent
                with Image.open(io.BytesIO(data)) as child:
                    canvas.paste(child.convert("RGBA"), (dx * tile_size, (1 - dy) * tile_size))
        with canvas.convert("RGBa") as premultiplied, premultiplied.reduce(2) as reduced:
            with reduced.convert("RGBA") as result:
                source = encode_tile(result, LOSSLESS_SOURCE) if enc["quality"] is not None else None
                return z, x, y, encode_tile(result, enc), source


def encoding(args):
    """Picklable encoder settings shared by base and overview workers."""
    return {"format": args.format, "quality": args.quality, "webp_method": args.webp_method,
            "png_compress_level": args.png_compress_level}


def tile_rows_worker(jobs, results, cfg):
    """Worker process: rectangles (c0, c1, r0, r1) from `jobs` -> encoded TMS tiles on `results`.

    Rectangles are aligned to blocks of 2**depth tiles, so every parent down to
    `depth` levels has all its descendants in one rectangle. Pairs of tile rows
    are reduced to the next level as soon as both are read, from the pixels
    that were just encoded, never from decoded (possibly lossy) tiles; only one
    pending row per level is held.
    """
    try:
        import signal

        import numpy as np
        from osgeo import gdal, ogr
        from PIL import Image

        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent stops workers
        gdal.UseExceptions()
        gdal.SetCacheMax(WORKER_CACHE_MIB * 1024 * 1024)
        ds = gdal.Open(cfg["vrt"])
        bands = ds.RasterCount
        cut_ds = ogr.Open(cfg["cut"]) if cfg["cut"] else None
        layer = cut_ds.GetLayer("cutline") if cut_ds else None
        mem = gdal.GetDriverByName("MEM")
        ts, res, (ox, oy) = cfg["tile_size"], cfg["res"], cfg["origin"]
        px0, py0, width, height = cfg["px0"], cfg["py0"], cfg["width"], cfg["height"]
        zoom, depth, threshold = cfg["zoom"], cfg["depth"], cfg["binary_alpha"]

        lossy = cfg["quality"] is not None

        def encode(pixels, enc=cfg):
            return encode_tile(Image.fromarray(np.ascontiguousarray(pixels), "RGBA"), enc)

        def read_row(ty, c0, c1):
            """One row of base tiles as RGBA; zeros outside the warp."""
            canvas = np.zeros((ts, (c1 - c0) * ts, 4), np.uint8)
            x, y = c0 * ts - px0, ty * ts - py0
            rx0, rx1 = max(0, x), min(width, c1 * ts - px0)
            ry0, ry1 = max(0, y), min(height, y + ts)
            if rx1 <= rx0 or ry1 <= ry0:
                return canvas
            view = canvas[ry0 - y:ry1 - y, rx0 - x:rx1 - x]
            data = ds.ReadAsArray(rx0, ry0, rx1 - rx0, ry1 - ry0)
            if bands == 2:  # gray + alpha
                view[..., :3] = data[0][..., None]
                view[..., 3] = data[1]
            else:
                view[...] = data.transpose(1, 2, 0)
            if layer is not None:
                mask = mem.Create("", rx1 - rx0, ry1 - ry0, 1, gdal.GDT_Byte)
                mask.SetGeoTransform((ox + rx0 * res, res, 0, oy - ry0 * res, 0, -res))
                gdal.RasterizeLayer(mask, [1], layer, burn_values=[255])  # pixel-centre rule
                np.minimum(view[..., 3], mask.ReadAsArray(), out=view[..., 3])
            return canvas

        def base_tiles(canvas, ty, c0, tiles, stats):
            """Encode the base row; return which tiles were stored (binary alpha applied in place)."""
            stored = np.zeros(canvas.shape[1] // ts, bool)
            for i in range(len(stored)):
                pixels = canvas[:, i * ts:(i + 1) * ts]
                alpha = pixels[..., 3]
                if not alpha.any():
                    if not cfg["keep_empty"] or not cfg["inside"](ty, c0 + i):
                        stats["empty_skipped"] += cfg["inside"](ty, c0 + i)
                        continue
                elif threshold and ((alpha > 0) & (alpha < 255)).any():
                    alpha[...] = np.where(alpha >= threshold, 255, 0)
                    stats["binary_alpha_tiles"] += 1
                    if not alpha.any() and not cfg["keep_empty"]:
                        stats["empty_skipped"] += 1
                        continue
                tiles.append((zoom, c0 + i, 2 ** zoom - 1 - ty, encode(pixels)))
                stored[i] = True
            return stored

        def reduce_rows(top, bottom):
            """Two rows of tiles -> one row at the next level: premultiplied exact 2x2 average."""
            with Image.fromarray(np.concatenate([top, bottom]), "RGBA") as im:
                with im.convert("RGBa") as premultiplied, premultiplied.reduce(2) as reduced:
                    with reduced.convert("RGBA") as result:
                        return np.asarray(result).copy()

        while (job := jobs.get()) is not None:
            c0, c1, r0, r1 = job
            pending = [None] * (depth + 1)  # per level: (pixels, stored) of an upper row awaiting its pair
            for ty in range(r0, r1):
                tiles, source, stats = [], [], {"empty_skipped": 0, "binary_alpha_tiles": 0, "overview_tiles": 0}
                canvas = read_row(ty, c0, c1)
                row = (canvas, base_tiles(canvas, ty, c0, tiles, stats))
                level, row_index, col0 = 0, ty, c0
                if lossy and depth == 0:  # the base itself is the lowest level the workers build
                    source += [t[:3] + (encode(row[0][:, (t[1] - c0) * ts:(t[1] - c0 + 1) * ts], LOSSLESS_SOURCE),)
                               for t in tiles]
                while level < depth:
                    if pending[level] is None:
                        pending[level] = row
                        break
                    (top, top_stored), (bottom, bottom_stored) = pending[level], row
                    pending[level] = None
                    level, row_index, col0 = level + 1, row_index // 2, col0 // 2
                    stored = (top_stored | bottom_stored).reshape(-1, 2).any(axis=1)
                    pixels = reduce_rows(top, bottom)
                    z = zoom - level
                    for i in np.flatnonzero(stored).tolist():  # Python ints: numpy ints are stored as BLOBs
                        tile_pixels = pixels[:, i * ts:(i + 1) * ts]
                        key = (z, col0 + i, 2 ** z - 1 - row_index)
                        tiles.append(key + (encode(tile_pixels),))
                        if lossy and level == depth:  # lower levels are built from this, never from lossy tiles
                            source.append(key + (encode(tile_pixels, LOSSLESS_SOURCE),))
                    stats["overview_tiles"] += int(stored.sum())
                    row = (pixels, stored)
                results.put(("tiles", (tiles, source), stats))
            results.put(("rect", job, None))  # every row of it is on the queue before this
        results.put(("done", None, None))
    except BaseException:
        results.put(("error", traceback.format_exc(), None))


def plan_rectangles(layout, threads, depth):
    """Rectangles (c0, c1, r0, r1) aligned to blocks of 2**depth tiles; return (rectangles, rows of work)."""
    block = 2 ** depth
    tx0, ty0 = layout["tx0"] // block * block, layout["ty0"] // block * block
    tx1, ty1 = (layout["tx1"] // block + 1) * block, (layout["ty1"] // block + 1) * block  # exclusive
    columns, rows = tx1 - tx0, ty1 - ty0
    band = columns if columns <= READ_COLUMNS_MAX else max(READ_COLUMNS_WIDE, block)
    bands = [(c, min(c + band, tx1)) for c in range(tx0, tx1, band)]
    step = max(1, math.ceil(rows * len(bands) / (threads * JOBS_PER_WORKER) / block)) * block
    rectangles = [(c0, c1, r, min(r + step, ty1)) for c0, c1 in bands for r in range(ty0, ty1, step)]
    return rectangles, rows * len(bands)


def plan_base(layout, threads, max_depth):
    """(depth, rectangles, rows of work) for the base pass.

    Alignment to blocks of 2**depth tiles can leave a small raster (one plan)
    with fewer rectangles than workers; the depth is then lowered until every
    worker has at least MIN_JOBS_PER_WORKER rectangles, so pixels and speed
    do not depend on it.
    """
    if layout["width"] <= 0 or layout["height"] <= 0:
        raise TileError("Warped raster is empty")
    for depth in range(max_depth, -1, -1):
        rectangles, total = plan_rectangles(layout, threads, depth)
        if len(rectangles) >= threads * MIN_JOBS_PER_WORKER:
            break
    return depth, rectangles, total


def write_base(db, vrt, cut, layout, args, plan, done):
    """Base tiles and `depth` levels below them from worker processes; return stats.

    `plan` is (depth, rectangles, rows) fixed when the output was started, so a
    resumed run cuts the same rectangles whatever --threads it gets. Rectangles
    in `done` are skipped; each finished one is recorded in the transaction of
    its last row, and rows of an unfinished one are simply written again
    (tiles are deterministic). This process is the only SQLite writer.
    """
    depth, rectangles, total = plan
    rectangles = [r for r in rectangles if tuple(r) not in done]
    stats = {"tiles": 0, "empty_skipped": 0, "binary_alpha_tiles": 0, "overview_tiles": 0}
    if not rectangles:
        return stats
    workers = min(args.threads, len(rectangles))
    ctx = multiprocessing.get_context("spawn")
    jobs, results = ctx.Queue(), ctx.Queue(maxsize=2 * workers)  # bounded: a slow writer slows the workers
    for rectangle in rectangles:
        jobs.put(rectangle)
    for _ in range(workers):
        jobs.put(None)
    cfg = {**layout, **encoding(args), "vrt": str(vrt), "cut": str(cut) if cut else None, "depth": depth,
           "binary_alpha": args.binary_alpha, "keep_empty": args.keep_empty,
           "inside": TileRange(layout)}
    procs = [ctx.Process(target=tile_rows_worker, args=(jobs, results, cfg), daemon=True) for _ in range(workers)]
    done_rows, start, last_log = 0, time.monotonic(), time.monotonic()
    for p in procs:
        p.start()
    try:
        finished = 0
        while finished < workers:
            try:
                kind, payload, counts = results.get(timeout=5)
            except queue.Empty:
                dead = [p.exitcode for p in procs if p.exitcode not in (None, 0)]
                if dead:
                    raise TileError(f"Tile worker died (exit code {dead[0]}; out of memory?)") from None
                continue
            if kind == "error":
                raise TileError("Tile worker failed: " + " | ".join(payload.strip().splitlines()[-3:]))
            if kind == "done":
                finished += 1
                continue
            if kind == "rect":
                db.execute("INSERT INTO _mbmerge_tile_done VALUES(?,?,?,?)", tuple(payload))
                db.commit()
                continue
            tiles, source = payload
            db.executemany("INSERT OR REPLACE INTO tiles VALUES(?,?,?,?)", tiles)
            db.executemany("INSERT OR REPLACE INTO _mbmerge_tile_src VALUES(?,?,?,?)", source)
            db.commit()
            stats["tiles"] += len(tiles) - counts["overview_tiles"]
            for key, value in counts.items():
                stats[key] += value
            done_rows += 1
            if time.monotonic() - last_log >= PROGRESS_SECONDS:
                elapsed = time.monotonic() - start
                log(args, "tiling", zoom=layout["zoom"], rows_done=done_rows, rows=total, tiles=stats["tiles"],
                    tiles_per_s=round(stats["tiles"] / elapsed, 1), seconds=round(elapsed))
                last_log = time.monotonic()
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
            p.join()
    log(args, "base", zoom=layout["zoom"], levels=depth, workers=workers,
        seconds=round(time.monotonic() - start, 1), **stats)
    return stats


class TileRange:
    """Picklable test: is XYZ tile (row, column) inside the warp's own tile range?

    Rectangles are padded to the block grid; padding tiles are neither counted
    as skipped nor stored by --keep-empty.
    """

    def __init__(self, layout):
        self.bounds = (layout["tx0"], layout["tx1"], layout["ty0"], layout["ty1"])

    def __call__(self, ty, tx):
        tx0, tx1, ty0, ty1 = self.bounds
        return tx0 <= tx <= tx1 and ty0 <= ty <= ty1


def children_of(db, z, x, y, table="tiles"):
    """The four child blobs of parent z/x/y (None where missing), in CHILD_OFFSETS order."""
    out = []
    for dx, dy in CHILD_OFFSETS:
        row = db.execute(f"SELECT tile_data FROM {table} WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                         (z + 1, 2 * x + dx, 2 * y + dy)).fetchone()
        out.append(row[0] if row else None)
    return tuple(out)


def write_overviews(db, zoom, min_zoom, args, done_levels=()):
    """Levels zoom-1 .. min_zoom from the level below, as `merge` builds its pyramid.

    The parent reads the children of the next parents while the workers encode,
    with at most OVERVIEW_INFLIGHT jobs per worker submitted at a time.
    """
    written = 0
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=args.threads, mp_context=ctx, initializer=worker_init) as pool:
        for z in range(zoom - 1, min_zoom - 1, -1):
            if z in done_levels:
                continue
            start = time.monotonic()
            parents = db.execute("SELECT DISTINCT tile_column / 2, tile_row / 2 FROM tiles WHERE zoom_level = ?",
                                 (z + 1,)).fetchall()
            # lossy output: children come from the lossless copies, so no level is built from decoded lossy tiles
            table = "_mbmerge_tile_src" if args.quality is not None else "tiles"
            pending, rows = deque(), []

            def drain(limit):
                while len(pending) > limit:
                    rows.append(pending.popleft().result())
                if len(rows) >= OVERVIEW_COMMIT or (limit == 0 and rows):
                    db.executemany("INSERT OR REPLACE INTO tiles VALUES(?,?,?,?)", [r[:4] for r in rows])
                    db.executemany("INSERT OR REPLACE INTO _mbmerge_tile_src VALUES(?,?,?,?)",
                                   [r[:3] + (r[4],) for r in rows if r[4] is not None])
                    db.commit()
                    rows.clear()

            for x, y in parents:
                pending.append(pool.submit(overview_tile, (z, x, y, children_of(db, z, x, y, table)), args.tile_size,
                                           encoding(args)))
                drain(OVERVIEW_INFLIGHT * args.threads)
            drain(0)
            db.execute("INSERT INTO _mbmerge_tile_state VALUES(?, '1')", (f"overview:{z}",))
            db.commit()
            written += len(parents)
            log(args, "overviews", zoom=z, tiles=len(parents), seconds=round(time.monotonic() - start, 1))
    return written


def wgs84_bounds(path, args):
    poly = info(path, args).get("wgs84Extent", {}).get("coordinates")
    if not poly:
        return None
    xs = [p[0] for p in poly[0]]
    ys = [p[1] for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


STATE_TABLES = ("_mbmerge_tile_state", "_mbmerge_tile_done", "_mbmerge_tile_src")


def open_output(tmp, layout, args, settings, inputs, max_depth):
    """New or resumed `<output>.tmp`; return (db, plan, finished rectangles, finished overview levels).

    A run that fails or is killed leaves the .tmp file; --resume continues it
    when the warp and every setting are the same, otherwise refuses.
    """
    contract = json.dumps({"settings": settings, "inputs": inputs, "layout": {k: v for k, v in layout.items() if k != "origin"},
                           "origin": list(layout["origin"])}, sort_keys=True)
    if args.resume and tmp.exists():
        db = sqlite3.connect(tmp)
        try:
            state = dict(db.execute("SELECT name, value FROM _mbmerge_tile_state"))
        except sqlite3.Error:
            db.close()
            raise TileError(f"{tmp} is not a resumable tile output; remove it or run without --resume") from None
        if state.get("contract") != contract:
            db.close()
            raise TileError(f"{tmp} was started with other inputs or settings; remove it or run without --resume")
        depth, rectangles, total = json.loads(state["plan"])
        done = {tuple(r) for r in db.execute("SELECT * FROM _mbmerge_tile_done")}
        levels = {int(k.split(":")[1]) for k in state if k.startswith("overview:")}
        log(args, "resume", rectangles_done=len(done), rectangles=len(rectangles), overview_levels_done=len(levels))
        return db, (depth, [tuple(r) for r in rectangles], total), done, levels
    tmp.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{tmp}{suffix}").unlink(missing_ok=True)
    plan = plan_base(layout, args.threads, max_depth)
    db = sqlite3.connect(tmp)
    db.execute(f"PRAGMA page_size={args.page_size}")
    db.executescript("""
        CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tiles(zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
          tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL);
        CREATE UNIQUE INDEX tile_index ON tiles(zoom_level, tile_column, tile_row);
        CREATE TABLE _mbmerge_tile_state(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE _mbmerge_tile_done(c0 INTEGER, c1 INTEGER, r0 INTEGER, r1 INTEGER);
        CREATE TABLE _mbmerge_tile_src(zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
          tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL);
        CREATE UNIQUE INDEX tile_src_index ON _mbmerge_tile_src(zoom_level, tile_column, tile_row);""")
    db.executemany("INSERT INTO _mbmerge_tile_state VALUES(?,?)",
                   [("contract", contract), ("plan", json.dumps(plan))])
    db.commit()
    return db, plan, set(), set()


def write_mbtiles(output, vrt, cut, zoom, min_zoom, args, settings, inputs=()):
    """Base tiles, overviews and metadata into `output` via `<output>.tmp` and rename.

    The .tmp file is committed after every row of tiles (WAL, synchronous=NORMAL:
    a killed process loses at most the rows in flight) and kept on failure, so
    `--resume` continues instead of starting over.
    """
    tmp = output.with_name(output.name + ".tmp")
    layout = tile_layout(info(vrt, args), zoom, args.tile_size)
    db, plan, done, levels = open_output(tmp, layout, args, settings, list(inputs), min(PYRAMID_DEPTH, zoom - min_zoom))
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        stats = write_base(db, vrt, cut, layout, args, plan, done)
        depth = plan[0]
        if not db.execute("SELECT 1 FROM tiles WHERE zoom_level = ? LIMIT 1", (zoom,)).fetchone():
            db.close()
            tmp.unlink(missing_ok=True)
            raise TileError("No nonempty tiles: the input is fully transparent after nodata/cutline handling")
        if min_zoom < zoom - depth:
            stats["overview_tiles"] += write_overviews(db, zoom - depth, min_zoom, args, levels)
        meta = {"name": args.name or output.stem, "format": args.format, "type": "overlay", "version": "1.3",
                "scheme": "tms", "minzoom": str(min_zoom), "maxzoom": str(zoom),
                "mbmerge:tile": json.dumps(settings, ensure_ascii=False),
                "mbmerge:empty_skipped": str(stats["empty_skipped"])}
        bounds = wgs84_bounds(vrt, args)
        if bounds:
            w, s, e, n = bounds
            meta["bounds"] = f"{w:.8f},{s:.8f},{e:.8f},{n:.8f}"
            meta["center"] = f"{(w + e) / 2:.8f},{(s + n) / 2:.8f},{zoom}"
        db.executemany("INSERT OR REPLACE INTO metadata VALUES(?,?)", meta.items())
        for table in STATE_TABLES:
            db.execute(f"DROP TABLE {table}")
        db.commit()
        db.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm next to it
        # no VACUUM: it would rewrite a country-sized file; the dropped pyramid source and rows replaced
        # on resume leave free pages of the order of 1 % (one tile in 4**PYRAMID_DEPTH)
    except BaseException:
        try:
            db.close()
        except sqlite3.Error:
            pass
        if tmp.exists():
            log(args, "kept", path=tmp, hint="rerun with --resume to continue")
        raise
    db.close()
    os.replace(tmp, output)
    return stats


def tile(args):
    """`mbmerge tile`: inputs -> flat MBTiles for `mbmerge merge`."""
    if args.quality is not None and args.format != "webp":
        raise TileError("--quality selects lossy WebP; it cannot be combined with --format png")
    version = gdal_version()
    output = Path(args.output).expanduser().resolve()
    if output.exists() and not args.overwrite:
        raise TileError(f"Output exists: {output}; use --overwrite")
    if not output.parent.is_dir():
        raise TileError(f"Output directory does not exist: {output.parent}")
    args.collar = [parse_color(c) for c in args.collar]
    args.nodata = [parse_color(c) for c in args.nodata]
    files = collect(args)
    with tempfile.TemporaryDirectory(prefix="mbmerge-tile-", dir=args.work_dir) as tmp:
        work = Path(tmp)
        prepared = [prepare_input(i, p, work, args) for i, p in enumerate(files)]
        if len(prepared) == 1:
            src = prepared[0]
        else:
            src = work / "mosaic.vrt"
            run(["gdalbuildvrt", "-q", "-addalpha", src, *prepared], args)
        native = native_resolution(src, work, args)
        zoom = args.zoom if args.zoom is not None else tile_zoom(native, args.tile_size)
        min_zoom = zoom if args.min_zoom is None else args.min_zoom
        if min_zoom > zoom:
            raise TileError("--min-zoom must be <= --zoom")
        target = WORLD / (args.tile_size * 2 ** zoom)
        resampling = args.resampling
        if resampling == "auto":
            resampling = "average" if native < target / 1.2 else "bilinear"
        warped, cut = warp(src, work, zoom, resampling, args)
        settings = {"gdal": version, "zoom": zoom, "min_zoom": min_zoom, "tile_size": args.tile_size,
                    "native_resolution_m": round(native, 4), "resampling": resampling,
                    "format": args.format, "lossless": args.quality is None, "quality": args.quality,
                    "pdf_dpi": args.pdf_dpi, "s_srs": args.s_srs,
                    "nodata": [list(c) for c in args.nodata],
                    "collar": [list(c) for c in args.collar], "near": args.near if args.collar else None,
                    "binary_alpha": args.binary_alpha, "cutline": bool(args.cutline), "cwhere": args.cwhere,
                    "inputs": len(files)}
        # path, size and mtime of every input: resuming over changed sources would mix two versions
        inputs = [[str(f), f.stat().st_size, f.stat().st_mtime_ns] for f in map(Path, files)]
        stats = write_mbtiles(output, warped, cut, zoom, min_zoom, args, settings, inputs)
    log(args, "tiled", output=output, zoom=zoom, min_zoom=min_zoom, resampling=resampling, **stats)
    return stats


def add_parser(sub, positive):
    t = sub.add_parser("tile", help="georeferenced rasters (TIFF, PDF, VRT, JPEG …) -> MBTiles for merge")
    t.add_argument("inputs", nargs="*", help="input rasters; later inputs are drawn over earlier ones")
    t.add_argument("--input-dir")
    t.add_argument("--pattern", default="*.tif", help="glob within --input-dir; ** supported")
    t.add_argument("--output", required=True)
    t.add_argument("--overwrite", action="store_true")
    t.add_argument("--resume", action="store_true",
                   help="continue <output>.tmp left by an interrupted run with the same inputs and settings")
    t.add_argument("--zoom", type=int, choices=range(31), metavar="Z",
                   help="base zoom; default: nearest to the source resolution")
    t.add_argument("--min-zoom", type=int, choices=range(31), metavar="Z",
                   help="lowest level to generate (e.g. 0 for previews); default: base zoom only")
    t.add_argument("--tile-size", type=positive, default=512)
    t.add_argument("--format", choices=("webp", "png"), default="webp", help="lossless unless --quality is given")
    t.add_argument("--quality", type=int, choices=QUALITY_RANGE, metavar="1-100",
                   help="lossy WebP at this quality (e.g. 85 for orthophotos); merge would store such tiles "
                        "losslessly re-encoded, so keep the default lossless output for layers you merge")
    t.add_argument("--webp-method", type=int, choices=range(7), default=4)
    t.add_argument("--png-compress-level", type=int, choices=range(10), default=6)
    t.add_argument("--resampling", choices=RESAMPLING, default="auto",
                   help="auto: average when the source is finer than the target, else bilinear")
    t.add_argument("--s-srs", metavar="SRS",
                   help="coordinate system for inputs that have none (world files), e.g. EPSG:5514")
    t.add_argument("--pdf-dpi", type=positive, default=300)
    t.add_argument("--cutline", help="polygon file (any OGR format); pixels outside become transparent")
    t.add_argument("--cutline-layer")
    t.add_argument("--cwhere", help="OGR SQL filter for --cutline, e.g. \"KOD = '530310'\"")
    t.add_argument("--nodata", action="append", default=[], metavar="R,G,B",
                   help="exact color treated as nodata EVERYWHERE in the input (repeatable)")
    t.add_argument("--collar", action="append", default=[], metavar="COLOR",
                   help="edge-connected collar color made transparent (nearblack): black, white or R,G,B; repeatable")
    t.add_argument("--near", type=int, default=15, help="collar color tolerance for lossy sources (default 15)")
    t.add_argument("--binary-alpha", type=int, nargs="?", const=128, metavar="THRESHOLD",
                   help="snap partial alpha to 0/255 (default threshold 128) so overlaying again is idempotent")
    t.add_argument("--keep-empty", action="store_true", help="store fully transparent tiles")
    t.add_argument("--page-size", type=int, choices=(1024, 2048, 4096, 8192, 16384, 32768, 65536), default=4096)
    t.add_argument("--threads", type=positive, default=min(8, os.cpu_count() or 1),
                   help="worker processes (each reads and encodes its own rows of tiles)")
    t.add_argument("--work-dir", help="directory for temporary files: rasterized PDFs, collar masks, VRTs "
                                      "(default: system temp)")
    t.add_argument("--name")
    t.add_argument("--json-log", action="store_true")
    t.set_defaults(func=tile)
    return t
