# SPDX-License-Identifier: MIT
"""Georeferenced rasters -> one flat, lossless MBTiles that `mbmerge merge` accepts.

Pipeline (GDAL >= 3.11 command-line tools, run as subprocesses; no Python
bindings are needed because they are tied to the system libgdal):

1. per input: PDF -> GeoTIFF at --pdf-dpi; edge-connected collars -> alpha
   (`nearblack -setalpha`, --collar); exact colors -> alpha everywhere
   (`gdalwarp -srcnodata ... -dstalpha`, --nodata). Declared nodata, masks and
   alpha bands of the source are always honoured by GDAL itself.
2. several inputs -> one mosaic (`gdalbuildvrt -addalpha`, later inputs on top).
3. one warp to EPSG:3857 at exactly the resolution of --zoom, aligned to the
   tile grid (-tap), optionally clipped by --cutline/--cwhere, alpha added.
4. `gdal raster tile` (TMS rows, nearest at the base level because pixels are
   already aligned, average for lower levels, blank tiles skipped).
5. packing: tiles that are fully transparent are dropped, --binary-alpha snaps
   partial alpha to 0/255, every tile is checked to be lossless.

Only fully transparent tiles count as empty. Colors are never interpreted at
packing time: turning black/white collars into transparency is step 1's job,
before any resampling can blend them into valid pixels.
"""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

from merge_mbtiles import image_format, lossless_webp, log

WORLD = 40075016.685578488  # EPSG:3857 extent width in metres
MIN_GDAL = (3, 11)
RESAMPLING = ("auto", "nearest", "bilinear", "cubic", "lanczos", "average")


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
    try:
        out = subprocess.run(["gdal", "--version"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise TileError("GDAL command-line tools not found; `mbmerge tile` needs GDAL >= 3.11 on PATH") from None
    match = re.search(r"GDAL (\d+)\.(\d+)", out)
    if not match or tuple(map(int, match.groups())) < MIN_GDAL:
        raise TileError(f"`mbmerge tile` needs GDAL >= 3.11 (found: {out.strip() or 'unknown'})")
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
    """Make one input ready for mosaicking: rasterized, collars/colors -> alpha."""
    src = path
    if path.suffix.lower() == ".pdf":
        src = work / f"{index}.tif"
        run(["gdal_translate", "-q", "--config", "GDAL_PDF_DPI", str(args.pdf_dpi), "-of", "GTiff",
             "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", path, src], args)
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


def warp(src, work, zoom, resampling, args):
    """Resample once into the tile grid, then clip in that same grid.

    gdalwarp evaluates a cutline in the pixel space of its *source*. Clipping
    during the reprojection would therefore sample each boundary on a different,
    document-specific grid, and neighbours clipped by one shared boundary would
    overlap or leave one-pixel cracks. Clipping the already aligned raster with
    nearest neighbour samples every boundary at the same pixel centres, so
    adjacent sources get exactly complementary pixels.
    """
    res = WORLD / (args.tile_size * 2 ** zoom)
    grid = ["-tr", repr(res), repr(res), "-tap"]
    aligned = work / "aligned.vrt"
    run(["gdalwarp", "-q", "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857", *grid, "-dstalpha",
         "-r", resampling, "-wm", "256", src, aligned], args)
    if not args.cutline:
        return aligned
    out = work / "clipped.vrt"
    cmd = ["gdalwarp", "-q", "-overwrite", "-of", "VRT", *grid, "-r", "near", "-dstalpha",
           "-cutline", Path(args.cutline).expanduser().resolve(strict=True), "-crop_to_cutline"]
    if args.cutline_layer:
        cmd += ["-cl", args.cutline_layer]
    if args.cwhere:
        cmd += ["-cwhere", args.cwhere]
    run(cmd + [aligned, out], args)
    return out


def pack(tiles_dir, output, args, zoom, bounds, settings):
    """Write gdal tiles into a flat MBTiles; return statistics."""
    from PIL import Image
    tmp = output.with_name(output.name + ".tmp")
    tmp.unlink(missing_ok=True)
    stats = {"tiles": 0, "empty_skipped": 0, "binary_alpha_tiles": 0, "reencoded": 0}
    db = sqlite3.connect(tmp)
    try:
        db.execute(f"PRAGMA page_size={args.page_size}")
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript("""
            CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tiles(zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
              tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL);
            CREATE UNIQUE INDEX tile_index ON tiles(zoom_level, tile_column, tile_row);""")
        ext = "webp" if args.format == "webp" else "png"
        zooms = set()
        for z_dir in sorted(tiles_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
            if not z_dir.name.isdigit():
                continue
            for x_dir in z_dir.iterdir():
                for f in x_dir.glob(f"*.{ext}"):
                    data = f.read_bytes()
                    z, x, y = int(z_dir.name), int(x_dir.name), int(f.stem)
                    with Image.open(io.BytesIO(data)) as im:
                        rgba = im.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    low, high = alpha.getextrema()
                    if high == 0 and not args.keep_empty:
                        stats["empty_skipped"] += 1
                        continue
                    changed = False
                    # Only the base level is merged; previews below it keep smooth averaged edges.
                    if (args.binary_alpha and z == zoom and (low, high) != (255, 255)
                            and any(alpha.histogram()[1:255])):
                        rgba.putalpha(alpha.point(lambda a: 255 if a >= args.binary_alpha else 0))
                        stats["binary_alpha_tiles"] += 1
                        changed = True
                        if rgba.getchannel("A").getextrema()[1] == 0 and not args.keep_empty:
                            stats["empty_skipped"] += 1
                            continue
                    if changed or image_format(data) != args.format or (
                            args.format == "webp" and not lossless_webp(data)):
                        out = io.BytesIO()
                        if args.format == "webp":
                            rgba.save(out, "WEBP", lossless=True, exact=True, method=args.webp_method)
                        else:
                            rgba.save(out, "PNG", compress_level=args.png_compress_level)
                        data = out.getvalue()
                        stats["reencoded"] += 1
                    db.execute("INSERT INTO tiles VALUES(?,?,?,?)", (z, x, y, data))
                    stats["tiles"] += 1
                    zooms.add(z)
        if not zooms:
            raise TileError("No nonempty tiles: the input is fully transparent after nodata/cutline handling")
        meta = {"name": args.name or output.stem, "format": args.format, "type": "overlay", "version": "1.3",
                "scheme": "tms", "minzoom": str(min(zooms)), "maxzoom": str(max(zooms)),
                "mbmerge:tile": json.dumps(settings, ensure_ascii=False),
                "mbmerge:empty_skipped": str(stats["empty_skipped"])}
        if bounds:
            w, s, e, n = bounds
            meta["bounds"] = f"{w:.8f},{s:.8f},{e:.8f},{n:.8f}"
            meta["center"] = f"{(w + e) / 2:.8f},{(s + n) / 2:.8f},{zoom}"
        db.executemany("INSERT INTO metadata VALUES(?,?)", meta.items())
        db.commit()
    finally:
        db.close()
    os.replace(tmp, output)
    return stats


def wgs84_bounds(path, args):
    poly = info(path, args).get("wgs84Extent", {}).get("coordinates")
    if not poly:
        return None
    xs = [p[0] for p in poly[0]]
    ys = [p[1] for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def tile(args):
    """`mbmerge tile`: inputs -> lossless flat MBTiles for `mbmerge merge`."""
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
        warped = warp(src, work, zoom, resampling, args)
        tiles_dir = work / "tiles"
        cmd = ["gdal", "raster", "tile", "--min-zoom", min_zoom, "--max-zoom", zoom,
               "--tile-size", args.tile_size, "--convention", "tms",
               "-r", "nearest", "--overview-resampling", "average",
               "--format", "WEBP" if args.format == "webp" else "PNG",
               "--skip-blank", "--webviewer", "none", "--num-threads", args.threads]
        if args.format == "webp":
            cmd += ["--co", "LOSSLESS=YES"]
        run(cmd + [warped, tiles_dir], args)
        settings = {"gdal": version, "zoom": zoom, "min_zoom": min_zoom, "tile_size": args.tile_size,
                    "native_resolution_m": round(native, 4), "resampling": resampling,
                    "format": args.format, "lossless": True, "pdf_dpi": args.pdf_dpi,
                    "nodata": [list(c) for c in args.nodata],
                    "collar": [list(c) for c in args.collar], "near": args.near if args.collar else None,
                    "binary_alpha": args.binary_alpha, "cutline": bool(args.cutline), "cwhere": args.cwhere,
                    "inputs": len(files)}
        stats = pack(tiles_dir, output, args, zoom, wgs84_bounds(warped, args), settings)
    log(args, "tiled", output=output, zoom=zoom, min_zoom=min_zoom, resampling=resampling, **stats)
    return stats


def add_parser(sub, positive):
    t = sub.add_parser("tile", help="georeferenced rasters (TIFF, PDF, VRT, JPEG …) -> lossless MBTiles for merge")
    t.add_argument("inputs", nargs="*", help="input rasters; later inputs are drawn over earlier ones")
    t.add_argument("--input-dir")
    t.add_argument("--pattern", default="*.tif", help="glob within --input-dir; ** supported")
    t.add_argument("--output", required=True)
    t.add_argument("--overwrite", action="store_true")
    t.add_argument("--zoom", type=int, choices=range(31), metavar="Z",
                   help="base zoom; default: nearest to the source resolution")
    t.add_argument("--min-zoom", type=int, choices=range(31), metavar="Z",
                   help="lowest level to generate (e.g. 0 for previews); default: base zoom only")
    t.add_argument("--tile-size", type=positive, default=512)
    t.add_argument("--format", choices=("webp", "png"), default="webp", help="always lossless")
    t.add_argument("--webp-method", type=int, choices=range(7), default=4)
    t.add_argument("--png-compress-level", type=int, choices=range(10), default=6)
    t.add_argument("--resampling", choices=RESAMPLING, default="auto",
                   help="auto: average when the source is finer than the target, else bilinear")
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
    t.add_argument("--threads", type=positive, default=min(8, os.cpu_count() or 1))
    t.add_argument("--work-dir", help="directory for temporary files (default: system temp)")
    t.add_argument("--name")
    t.add_argument("--json-log", action="store_true")
    t.set_defaults(func=tile)
    return t
