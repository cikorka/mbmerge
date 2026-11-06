# SPDX-License-Identifier: MIT
"""`mbmerge grade`: one colour grade for a whole mosaic, applied by `tile --grade` before encoding.

The grade is a pure per-pixel function, identical for every input, so it cannot create a
seam between sheets: levels (black and white point per channel), gamma on the result,
then saturation (linear, towards Rec. 709 luma) and vibrance (more boost for dull colours,
little for already saturated ones — keeps red roofs and blue pools from burning out).
Alpha is never touched. Levels and gamma are a 3 x 256 lookup table; saturation and
vibrance are a few integer operations per pixel.

`mbmerge grade` measures the levels from a sample of the whole mosaic (each input read at
1/8 resolution, which the JPEG driver decodes without the full image), takes percentiles
of the histogram per channel and writes the parameters as JSON. The same file can be reused
for another year so that years look alike. `--preview` renders before/after crops.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

GRADE_VERSION = 1
LUMA = (0.2126, 0.7152, 0.0722)  # Rec. 709
SAMPLE_SCALE = 8  # inputs read at 1/8 of their resolution
EDGE = 3  # pixels this close to 0 or 255 in every channel are collars/nodata, not image


class GradeError(Exception):
    pass


def load(path):
    grade = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(grade)
    return grade


def validate(grade):
    if grade.get("version") != GRADE_VERSION:
        raise GradeError(f"Unsupported grade version {grade.get('version')!r}; expected {GRADE_VERSION}")
    lo, hi = grade["black"], grade["white"]
    if len(lo) != 3 or len(hi) != 3 or any(not 0 <= a < b <= 255 for a, b in zip(lo, hi)):
        raise GradeError("black/white must be three channels with 0 <= black < white <= 255")
    if not 0.1 <= grade["gamma"] <= 10:
        raise GradeError("gamma must be within 0.1..10")
    if not 0 <= grade["saturation"] <= 4 or not -1 <= grade["vibrance"] <= 2:
        raise GradeError("saturation must be within 0..4 and vibrance within -1..2")


def contract(grade):
    """The part of a grade that decides pixels (resume contract, output metadata)."""
    return {k: grade[k] for k in ("version", "black", "white", "gamma", "saturation", "vibrance")}


def lut(grade):
    """3 x 256 uint8 table: levels per channel, then gamma (shared)."""
    import numpy as np

    x = np.arange(256, dtype=np.float64)
    table = np.empty((3, 256), np.uint8)
    for c in range(3):
        lo, hi = grade["black"][c], grade["white"][c]
        v = np.clip((x - lo) / (hi - lo), 0, 1) ** (1 / grade["gamma"])
        table[c] = np.rint(v * 255)
    return table


class Grader:
    """Callable applied in the tile workers to RGBA rows in place."""

    def __init__(self, grade):
        import numpy as np

        self.np = np
        self.table = lut(grade)
        self.saturation = grade["saturation"]
        self.vibrance = grade["vibrance"]
        self.weights = np.array([round(w * 1024) for w in LUMA], np.int32)  # sums to 1024

    def __call__(self, rgba):
        np = self.np
        rgb = rgba[..., :3]
        for c in range(3):
            rgb[..., c] = self.table[c][rgb[..., c]]
        if self.saturation == 1 and self.vibrance == 0:
            return rgba
        v = rgb.astype(np.int32)
        luma = (v @ self.weights) >> 10
        factor = np.full(luma.shape, self.saturation, np.float32)
        if self.vibrance:
            # chroma 0 (grey) gets the full vibrance, chroma 255 none of it
            chroma = (v.max(axis=-1) - v.min(axis=-1)).astype(np.float32) / 255
            factor = factor * (1 + self.vibrance * (1 - chroma))
        out = luma[..., None] + (v - luma[..., None]) * factor[..., None]
        rgb[...] = np.clip(np.rint(out), 0, 255).astype(np.uint8)
        return rgba


def sheet_histogram(path):
    """Per-channel histogram of one input at 1/SAMPLE_SCALE without edge collars, or None."""
    import numpy as np
    from osgeo import gdal

    with gdal.ExceptionMgr(useExceptions=False):  # broken EXIF blocks report errors, pixels read fine
        ds = gdal.Open(str(path))
        if ds is None or ds.RasterCount < 3:
            return None
        w, h = max(1, ds.RasterXSize // SAMPLE_SCALE), max(1, ds.RasterYSize // SAMPLE_SCALE)
        data = ds.ReadAsArray(0, 0, ds.RasterXSize, ds.RasterYSize, buf_xsize=w, buf_ysize=h, band_list=[1, 2, 3])
    if data is None:
        return None
    rgb = data.reshape(3, -1)
    keep = ~(np.all(rgb <= EDGE, axis=0) | np.all(rgb >= 255 - EDGE, axis=0))
    return np.stack([np.bincount(rgb[c][keep], minlength=256) for c in range(3)])


def histogram(files, args):
    """Histograms of all inputs summed; (hist[3][256], pixels). GDAL decodes outside the GIL: threads scale."""
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np

    hist = np.zeros((3, 256), np.int64)
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for h in pool.map(sheet_histogram, files):
            if h is not None:
                hist += h
    return hist, int(hist[0].sum())


def percentile(h, q):
    import numpy as np

    cdf = np.cumsum(h)
    return int(np.searchsorted(cdf, q * cdf[-1]))


def measure(args):
    """`mbmerge grade` without --preview: sample the mosaic, write the grade JSON."""
    from tile_mbtiles import collect, log

    files = collect(args)
    hist, pixels = histogram(files, args)
    if not pixels:
        raise GradeError("No image pixels in the sample")
    black = [percentile(hist[c], args.clip / 100) for c in range(3)]
    white = [percentile(hist[c], 1 - args.clip / 100) for c in range(3)]
    if args.linked_levels:  # one black and white point for all channels: keeps the colour balance as it is
        black, white = [min(black)] * 3, [max(white)] * 3
    white = [max(w, b + 16) for b, w in zip(black, white)]
    gamma = args.gamma
    if gamma == "auto":
        # median luma after levels goes to --target-median
        table = lut({"black": black, "white": white, "gamma": 1.0})
        luma_hist = [0] * 256
        for c, weight in enumerate(LUMA):
            for v, n in enumerate(hist[c]):
                luma_hist[int(table[c][v])] += weight * n
        median = max(1, percentile(luma_hist, 0.5)) / 255
        gamma = round(math.log(median) / math.log(args.target_median / 255), 3)
        gamma = min(max(gamma, 0.5), 2.0)
    grade = {"version": GRADE_VERSION, "black": black, "white": white, "gamma": float(gamma),
             "saturation": args.saturation, "vibrance": args.vibrance,
             "sample": {"inputs": len(files), "pixels": pixels, "scale": f"1/{SAMPLE_SCALE}", "clip_percent": args.clip,
                        "linked_levels": args.linked_levels}}
    validate(grade)
    Path(args.output).write_text(json.dumps(grade, indent=1) + "\n", encoding="utf-8")
    log(args, "grade", output=args.output, **{k: grade[k] for k in ("black", "white", "gamma", "saturation", "vibrance")})
    return grade


def preview(args):
    """Before/after crops at --at LON,LAT (z of --zoom) as side-by-side JPEGs and an index.html."""
    import numpy as np
    from osgeo import gdal, osr
    from PIL import Image

    from tile_mbtiles import WORLD, collect

    grade = load(args.preview)
    grader = Grader(grade)
    files = collect(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    vrt = str(out / "mosaic.vrt")
    gdal.BuildVRT(vrt, [str(f) for f in files], outputSRS=args.s_srs)
    wgs, merc = osr.SpatialReference(), osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    merc.ImportFromEPSG(3857)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_merc = osr.CoordinateTransformation(wgs, merc)
    res = WORLD / (512 * 2 ** args.zoom)
    rows = []
    for i, at in enumerate(args.at):
        lon, lat = map(float, at.split(","))
        x, y, _ = to_merc.TransformPoint(lon, lat)
        half = args.size / 2 * res
        ds = gdal.Warp("", vrt, format="MEM", dstSRS="EPSG:3857", outputBounds=(x - half, y - half, x + half, y + half),
                       width=args.size, height=args.size, resampleAlg="average", dstAlpha=True)
        before = np.ascontiguousarray(ds.ReadAsArray().transpose(1, 2, 0))
        after = grader(before.copy())
        pair = np.concatenate([before[..., :3], np.full((args.size, 8, 3), 255, np.uint8), after[..., :3]], axis=1)
        name = f"preview_{i}.jpg"
        Image.fromarray(pair).save(out / name, quality=92)
        rows.append(f'<h3>{at}</h3><img src="{name}" style="max-width:100%">')
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>mbmerge grade</title>"
        f"<p>vlevo zdroj, vpravo {json.dumps(contract(grade))}</p>" + "".join(rows), encoding="utf-8")


def run(args):
    from tile_mbtiles import TileError

    try:
        if args.preview:
            if not args.at or not args.out:
                raise GradeError("--preview needs --at LON,LAT and --out DIR")
            preview(args)
        else:
            if not args.output:
                raise GradeError("--output grade.json is required")
            measure(args)
    except GradeError as exc:
        raise TileError(str(exc)) from exc


def add_parser(sub):
    g = sub.add_parser("grade", help="measure one colour grade for a whole mosaic (for tile --grade), or preview it")
    g.add_argument("inputs", nargs="*", help="source rasters")
    g.add_argument("--input-dir")
    g.add_argument("--pattern", default="*.tif", help="glob within --input-dir; ** supported")
    g.add_argument("--s-srs", metavar="SRS", help="coordinate system of sources without one (world files)")
    g.add_argument("--output", help="grade JSON to write")
    g.add_argument("--clip", type=float, default=0.3,
                   help="percent of the darkest and brightest sampled pixels clipped by the levels (default 0.3)")
    g.add_argument("--linked-levels", action="store_true",
                   help="one black/white point for all channels (no white balance correction)")
    g.add_argument("--gamma", type=lambda v: v if v == "auto" else float(v), default=1.0,
                   help="gamma after levels; auto = median luma to --target-median (default 1.0)")
    g.add_argument("--target-median", type=float, default=118, help="median luma for --gamma auto (default 118)")
    g.add_argument("--saturation", type=float, default=1.0, help="1 = unchanged (default)")
    g.add_argument("--vibrance", type=float, default=0.0, help="extra saturation for dull colours, 0 = none")
    g.add_argument("--threads", type=int, default=8, help="inputs read in parallel (default 8)")
    g.add_argument("--preview", metavar="GRADE_JSON", help="render before/after crops of this grade instead")
    g.add_argument("--at", action="append", default=[], metavar="LON,LAT", help="preview location (repeatable)")
    g.add_argument("--zoom", type=int, default=18, help="preview zoom (default 18)")
    g.add_argument("--size", type=int, default=1024, help="preview crop size in pixels (default 1024)")
    g.add_argument("--out", help="preview directory")
    g.set_defaults(func=run, json_log=False)
    return g
