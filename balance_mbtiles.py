# SPDX-License-Identifier: MIT
"""`mbmerge balance`: one exposure and colour for a mosaic of sheets flown on different days;
`tile --balance` applies it while reading.

A national orthophoto is delivered as a grid of map sheets cut from flights of different days.
Inside a flight block neighbouring sheets continue each other exactly; blocks differ in haze and
exposure, so they show as rectangles of another tone, and where two blocks meet on a sheet edge
the edge shows as a straight line. A global grade (`mbmerge grade`) cannot remove either: it is
one function for every pixel. Matching the tone across the seams alone is not enough either: a
flight in the harvest next to a spring one differs in its fields, and matching those would light
up or darken the whole block. The colour is therefore anchored on land that looks the same on any
summer day, forests and built-up areas, taken from a land cover raster (`--landcover`, by default
read as ESA WorldCover classes 10 and 50).

1. Every sheet is read once at `--sample` units per pixel (through its overviews). It keeps
   * a strip of 4 pixels along each of its four edges, averaged in bins of 4 pixels along the edge,
     with a flag for bins on invariant land; strips on either side of a common edge show the same
     ground a few metres apart;
   * the 10th to 90th percentiles of every channel over each invariant class (at `--class-sample`).
2. Anchors: the percentiles of each class over all sheets give one national target. Per sheet and
   channel a line maps the sheet's percentiles of both classes onto the target (gain and offset):
   a hazy sheet has bright, flat forests and gets more contrast and less offset.
3. Seams: at nodes every `--node` units along every common edge, a robust orthogonal regression
   of the strips within `--window` gives the gain ratio and the means on both sides. An edge inside
   a flight block (the regression is the identity) uses all bins; an edge between flights uses only
   bins on invariant land, so fields that changed between the flights do not count.
4. Every sheet carries a grid of nodes with a log gain and an offset per channel, solved for all
   sheets together as one sparse least-squares problem per quantity (a membrane): at seam nodes the
   corrections of both sheets cancel the step, inside a sheet neighbouring nodes agree, and every
   node is pulled towards the anchor of its sheet with `--anchor-weight`. Many sheets of one flight
   share one level that way, and the seams between flights close exactly.

The result is a .npz file: the sheet grid, the node grids of every sheet and statistics.
`tile --balance` evaluates it at the centre of every output pixel from its position: the sheet it
belongs to and the bilinear interpolation of that sheet's nodes. What cannot be levelled is content
that differs between flights (harvested fields next to green ones): forests, towns and exposure are
matched, not the season.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

BALANCE_VERSION = 2
EDGE = 3  # all channels this close to 0 or 255: collar or nodata, not image
STRIP = 4  # pixels along an edge kept on each side
BIN = 4  # pixels averaged along an edge
MIN_BINS = 40  # fewer valid bins in a window: no equation for that seam node
QUANTILES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)  # of every channel over an invariant class
MIN_CLASS = 200  # class pixels (at --class-sample) a sheet needs for that class to anchor it
GAIN_LIMIT = (0.6, 1.6)  # anchor gains outside are clipped: a sheet that far off is content, not haze
INSIDE_STEP = 4.0  # an edge whose fit moves the means less than this and the gain less than
INSIDE_GAIN = 0.08  # this (log) is inside one flight: both sides are one image
INSIDE_INLIERS = 0.8  # share of bins the regression keeps for an edge to count as inside one flight
FULL_RANGE = 60.0  # spread (10th to 90th percentile) of a seam window over which its gain counts fully
GUESSED_SEAM = 0.25  # weight of an edge between flights fitted on all its bins (no invariant land near it)
CONDITIONING = 1e-3  # pull of every node towards no change: only fixes sheets with no other equation
LATTICE = 16  # the correction is evaluated exactly at every 16th pixel and interpolated inside one sheet
HALF = math.pi * 6378137  # half the EPSG:3857 extent: the global pixel grid the lattice is anchored to


class BalanceError(Exception):
    pass


# ---------------------------------------------------------------- the balance file

def load(path):
    """The balance file as a dict of arrays plus 'meta' (dict)."""
    import numpy as np

    try:
        with np.load(path, allow_pickle=False) as z:
            data = {k: z[k] for k in z.files}
        meta = json.loads(str(data.pop("meta")))
    except (OSError, ValueError, KeyError) as exc:
        raise BalanceError(f"{path} is not a balance file written by `mbmerge balance` ({exc})") from None
    if meta.get("version") != BALANCE_VERSION:
        raise BalanceError(f"Unsupported balance version {meta.get('version')!r}; expected {BALANCE_VERSION}")
    data["meta"] = meta
    return data


def contract(path):
    """What decides the pixels of `tile --balance` (resume contract, output metadata)."""
    meta = load(path)["meta"]
    return {"version": meta["version"], "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "sheets": meta["sheets"], "node": meta["node"], "classes": meta["classes"]}


class Balancer:
    """Callable applied to RGBA pixels in place, given the EPSG:3857 position of their top-left corner.

    Every pixel is assigned to the sheet that contains its centre and corrected with the bilinear
    interpolation of that sheet's nodes. The correction is evaluated exactly at every LATTICE-th
    pixel of the global pixel grid of the resolution, so the lattice does not depend on how the
    image is split into rows or tiles, and interpolated inside lattice cells whose four corners lie
    in one sheet (it changes over hundreds of metres there). In cells crossed by a sheet edge every
    pixel is evaluated on its own, so the correction jumps exactly at the edge.
    """

    def __init__(self, path):
        import numpy as np
        from osgeo import osr

        data = load(path)
        meta = data["meta"]
        self.np = np
        nodes = data["nodes"].astype(np.float64)  # (n, NY, NX, 3, 2): log gain, offset
        self.n, self.ny, self.nx = nodes.shape[:3]
        flat = np.empty((self.n + 1, self.ny, self.nx, 6), np.float32)  # gain, offset per channel
        flat[:self.n, ..., 0::2], flat[:self.n, ..., 1::2] = np.exp(nodes[..., 0]), nodes[..., 1]
        flat[self.n, ..., 0::2], flat[self.n, ..., 1::2] = 1, 0  # the sheet "outside the grid": no change
        self.nodes = flat.reshape(-1, 6)
        self.ox, self.oy = meta["origin"]
        self.sw, self.sh = meta["sheet_size"]
        cols, rows = data["cols"], data["rows"]
        self.lut = np.full((rows.max() + 1, cols.max() + 1), self.n, np.int64)
        self.lut[rows, cols] = np.arange(len(cols))
        merc, srs = osr.SpatialReference(), osr.SpatialReference()
        merc.ImportFromEPSG(3857)
        srs.ImportFromWkt(meta["srs"])
        for s in (merc, srs):
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.ct = osr.CoordinateTransformation(merc, srs)

    def evaluate(self, fc, fr):
        """Sheet (n outside the grid) and correction (..., 6) at grid coordinates in sheet units."""
        np = self.np
        col, row = np.floor(fc).astype(np.int64), np.floor(fr).astype(np.int64)
        inside = (col >= 0) & (row >= 0) & (col < self.lut.shape[1]) & (row < self.lut.shape[0])
        idx = np.where(inside, self.lut[np.clip(row, 0, self.lut.shape[0] - 1), np.clip(col, 0, self.lut.shape[1] - 1)],
                       self.n)
        u, v = (fc - col) * (self.nx - 1), (fr - row) * (self.ny - 1)
        ix = np.clip(np.floor(u).astype(np.int64), 0, self.nx - 2)
        iy = np.clip(np.floor(v).astype(np.int64), 0, self.ny - 2)
        tx, ty = (u - ix).astype(np.float32)[..., None], (v - iy).astype(np.float32)[..., None]
        base = (idx * self.ny + iy) * self.nx + ix
        top = self.nodes[base] * (1 - tx) + self.nodes[base + 1] * tx
        bottom = self.nodes[base + self.nx] * (1 - tx) + self.nodes[base + self.nx + 1] * tx
        return idx, top * (1 - ty) + bottom * ty

    def __call__(self, rgba, x0, y0, res, chunk=64):
        np = self.np
        height, width = rgba.shape[:2]
        j0, i0 = math.floor((x0 + HALF) / res + 0.5), math.floor((HALF - y0) / res + 0.5)
        dx, dy = x0 + HALF - j0 * res, HALF - y0 - i0 * res  # sub-pixel offset of the image from the global grid
        # lattice points up to the first one past the last pixel: every pixel lies in a cell with both ends
        lj = np.arange(j0 // LATTICE * LATTICE, j0 + width + LATTICE, LATTICE)
        li = np.arange(i0 // LATTICE * LATTICE, i0 + height + LATTICE, LATTICE)
        gx, gy = np.meshgrid(-HALF + dx + (lj + 0.5) * res, HALF - dy - (li + 0.5) * res)
        pts = np.asarray(self.ct.TransformPoints(np.c_[gx.ravel(), gy.ravel()]))
        fc_l = ((pts[:, 0] - self.ox) / self.sw).reshape(gx.shape)
        fr_l = ((self.oy - pts[:, 1]) / self.sh).reshape(gx.shape)
        idx_l, corr_l = self.evaluate(fc_l, fr_l)
        one = idx_l[:-1, :-1]
        uniform = (one < self.n) & (one == idx_l[1:, :-1]) & (one == idx_l[:-1, 1:]) & (one == idx_l[1:, 1:])
        cj, tj = np.divmod(j0 + np.arange(width) - lj[0], LATTICE)
        ci, ti = np.divmod(i0 + np.arange(height) - li[0], LATTICE)
        tj, ti = (tj / LATTICE).astype(np.float32), (ti / LATTICE).astype(np.float32)
        w = tj[None, :, None]
        along = corr_l[:, cj] * (1 - w) + corr_l[:, cj + 1] * w  # (lattice rows, width, 6)
        fc_a = fc_l[:, cj] * (1 - tj) + fc_l[:, cj + 1] * tj  # grid coordinates for the exact pixels
        fr_a = fr_l[:, cj] * (1 - tj) + fr_l[:, cj + 1] * tj
        for r0 in range(0, height, chunk):  # bounded temporaries
            view = rgba[r0:r0 + chunk]
            c, t = ci[r0:r0 + chunk], ti[r0:r0 + chunk]
            corr = along[c] * (1 - t)[:, None, None] + along[c + 1] * t[:, None, None]
            opaque = view[..., 3] > 0
            rr, cc = np.nonzero(~uniform[c][:, cj] & opaque)  # cells crossed by a sheet edge
            if len(rr):
                tt = t[rr].astype(np.float64)
                fc = fc_a[c[rr], cc] * (1 - tt) + fc_a[c[rr] + 1, cc] * tt
                fr = fr_a[c[rr], cc] * (1 - tt) + fr_a[c[rr] + 1, cc] * tt
                corr[rr, cc] = self.evaluate(fc, fr)[1]
            rgb = view[..., :3].astype(np.float32)
            out = np.clip(np.rint(rgb * corr[..., 0::2] + corr[..., 1::2]), 0, 255).astype(np.uint8)
            view[..., :3] = np.where(opaque[..., None], out, view[..., :3])
        return rgba


# ---------------------------------------------------------------- the sheet grid

def sheet_header(path, s_srs):
    from osgeo import gdal, osr

    # Exceptions off for the whole header: a broken EXIF block reports its error late (on the
    # first metadata call), although the image and its georeferencing read fine.
    with gdal.ExceptionMgr(useExceptions=False):
        ds = gdal.Open(str(path))
        if ds is None:
            raise BalanceError(f"Cannot open {path}")
        if ds.RasterCount < 3:
            raise BalanceError(f"{path}: balance needs RGB inputs")
        srs = ds.GetSpatialRef()
        gt, w, h = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
    if srs is None:
        if not s_srs:
            raise BalanceError(f"{path} has no coordinate system; give --s-srs")
        srs = osr.SpatialReference()
        srs.SetFromUserInput(s_srs)
    if gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0:
        raise BalanceError(f"{path} is not north-up")
    return srs.ExportToWkt(), gt[0], gt[3], gt[1] * w, -gt[5] * h, gt[1]


def sheet_grid(files, s_srs, threads):
    """Inputs as a regular grid of equal north-up sheets: origin, sheet size, (col, row) of each, SRS."""
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    from osgeo import osr

    with ThreadPoolExecutor(max_workers=threads) as pool:
        heads = list(pool.map(lambda f: sheet_header(f, s_srs), files))
    first = osr.SpatialReference()
    first.ImportFromWkt(heads[0][0])
    for f, h in zip(files, heads):
        other = osr.SpatialReference()
        other.ImportFromWkt(h[0])
        if not first.IsSame(other):
            raise BalanceError(f"{f} has another coordinate system than {files[0]}")
    left, top = np.array([h[1] for h in heads]), np.array([h[2] for h in heads])
    width, height = np.array([h[3] for h in heads]), np.array([h[4] for h in heads])
    sw, sh = float(np.median(width)), float(np.median(height))
    pixel = min(h[5] for h in heads)
    tol = pixel  # georeferencing written to a few decimals: within one input pixel
    if np.abs(width - sw).max() > tol or np.abs(height - sh).max() > tol:
        raise BalanceError("balance needs inputs of one size (a grid of map sheets)")
    ox, oy = float(left.min()), float(top.max())
    fc, fr = (left - ox) / sw, (oy - top) / sh
    cols, rows = np.rint(fc).astype(np.int64), np.rint(fr).astype(np.int64)
    if np.abs(fc - cols).max() * sw > tol or np.abs(fr - rows).max() * sh > tol:
        raise BalanceError("balance needs inputs that tile a regular grid (edges of neighbours must coincide)")
    if len(set(zip(cols.tolist(), rows.tolist()))) != len(files):
        raise BalanceError("two inputs occupy the same place in the grid")
    return {"origin": (ox, oy), "sheet_size": (sw, sh), "cols": cols, "rows": rows, "srs": heads[0][0]}


# ---------------------------------------------------------------- reading the sheets

def read_sheet(path, bounds, width, height, srs, args):
    """One sheet at width x height: edge strips, their invariant flags and the class percentiles.

    strips: (top, bottom, left, right), each (bins, 3) means with NaN where invalid;
    flags: the same four as booleans, True on invariant land; quantiles: (classes, 9, 3) with NaN
    for a class the sheet has too little of.
    """
    import numpy as np
    from osgeo import gdal

    with gdal.ExceptionMgr(useExceptions=False):
        ds = gdal.Open(str(path))
        data = None if ds is None else ds.ReadAsArray(0, 0, ds.RasterXSize, ds.RasterYSize, buf_xsize=width,
                                                      buf_ysize=height, band_list=[1, 2, 3],
                                                      resample_alg=gdal.GRIORA_Average)
    if data is None:
        raise BalanceError(f"Cannot read {path}")
    rgb = data.transpose(1, 2, 0).astype(np.float32)
    valid = ~(np.all(rgb <= EDGE, -1) | np.all(rgb >= 255 - EDGE, -1))

    f = max(1, round(args.class_sample / args.sample))  # sample pixels per class pixel
    cw, ch = max(1, width // f), max(1, height // f)
    left, top, right, bottom = bounds
    cover = gdal.Warp("", args.landcover, format="MEM", dstSRS=srs, outputBounds=(left, bottom, right, top),
                      width=cw, height=ch, resampleAlg="mode")
    if cover is None:
        raise BalanceError(f"Cannot read {args.landcover} over {path}")
    lc = cover.ReadAsArray()
    invariant = np.isin(lc, args.classes)

    def binned(strip, ok):  # strip (STRIP, L, 3) -> (L // BIN, 3)
        n = strip.shape[1] // BIN
        s = (strip[:, :n * BIN] * ok[:, :n * BIN, None]).reshape(STRIP, n, BIN, 3).sum((0, 2))
        c = ok[:, :n * BIN].reshape(STRIP, n, BIN).sum((0, 2))
        out = s / np.maximum(c, 1)[:, None]
        out[c < STRIP * BIN // 2] = np.nan
        return out

    def flags(line):  # invariant flag of every bin along an edge, from the class pixels on that edge
        centres = (np.arange(len(line) * f // BIN) * BIN + BIN // 2) // f
        return line[np.minimum(centres, len(line) - 1)]

    strips = (binned(rgb[:STRIP], valid[:STRIP]), binned(rgb[-STRIP:], valid[-STRIP:]),
              binned(rgb[:, :STRIP].transpose(1, 0, 2), valid[:, :STRIP].T),
              binned(rgb[:, -STRIP:].transpose(1, 0, 2), valid[:, -STRIP:].T))
    edge_flags = tuple(flags(line)[:len(s)] for line, s in zip(
        (invariant[0], invariant[-1], invariant[:, 0], invariant[:, -1]), strips))

    small = rgb[:ch * f, :cw * f].reshape(ch, f, cw, f, 3).mean((1, 3))
    ok = valid[:ch * f, :cw * f].reshape(ch, f, cw, f).all((1, 3))
    quantiles = np.full((len(args.classes), len(QUANTILES), 3), np.nan, np.float32)
    for k, cls in enumerate(args.classes):
        m = ok & (lc == cls)
        if m.sum() >= MIN_CLASS:
            quantiles[k] = np.quantile(small[m], QUANTILES, axis=0)
    return {"strips": strips, "flags": edge_flags, "quantiles": quantiles}


def read_sheets(files, grid, args, started):
    """read_sheet for every input, in parallel (GDAL decodes outside the GIL)."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tile_mbtiles import log

    sw, sh = grid["sheet_size"]
    ox, oy = grid["origin"]
    pw, ph = max(BIN * 2, round(sw / args.sample)), max(BIN * 2, round(sh / args.sample))
    log(args, "balance_read", inputs=len(files), sheet_px=[pw, ph], sample=args.sample)

    def one(k):
        left, top = ox + grid["cols"][k] * sw, oy - grid["rows"][k] * sh
        return read_sheet(files[k], (left, top, left + sw, top - sh), pw, ph, grid["srs"], args)

    sheets = [None] * len(files)
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for k, sheet in enumerate(pool.map(one, range(len(files)))):
            sheets[k] = sheet
            if (k + 1) % 2000 == 0:
                log(args, "balance_read", read=k + 1, inputs=len(files), seconds=round(time.monotonic() - started))
    return sheets


# ---------------------------------------------------------------- anchors and seams

def anchors(quantiles):
    """Per sheet and channel the line onto the national percentiles of the invariant classes.

    quantiles (n, classes, 9, 3) -> gain (n, 3), offset (n, 3), weight of the gain (n), whether
    the sheet has any anchor (n), the national target (classes, 9, 3).
    """
    import warnings

    import numpy as np

    with warnings.catch_warnings():  # a class no sheet has: NaN target, never used
        warnings.simplefilter("ignore", RuntimeWarning)
        target = np.nanmedian(quantiles, axis=0)
    has = ~np.isnan(quantiles[:, :, 0, 0])
    n = len(quantiles)
    gain, offset = np.ones((n, 3)), np.zeros((n, 3))
    gain_weight = np.where(has.sum(1) >= 2, 1.0, 0.2)  # one class spans a narrow range: its gain is a guess
    for i in np.flatnonzero(has.any(1)):
        x, y = quantiles[i, has[i]].reshape(-1, 3), target[has[i]].reshape(-1, 3)
        for c in range(3):
            if has[i].sum() >= 2 and x[:, c].std() > 8:  # dark forest to bright roofs: gain and offset
                a = np.polyfit(x[:, c], y[:, c], 1)[0]
            else:  # one class: the gain from its spread
                a = (y[:, c].std() + 1) / (x[:, c].std() + 1)
            gain[i, c] = np.clip(a, *GAIN_LIMIT)
            offset[i, c] = np.median(y[:, c] - gain[i, c] * x[:, c])
    return gain, offset, gain_weight, has.any(1), target


def seams(sheets, grid, args):
    """Seam nodes between edge-sharing sheets with their fitted equation.

    Returns node indices on both sides, fits (k, 3, 5: alpha, texture, inliers, mean x, mean y),
    the strips (k, L, 3) of both sides for the statistics, the invariant flags, whether the edge
    lies inside one flight and the weight of every equation.
    """
    import warnings

    import numpy as np

    n = len(sheets)
    sw, sh = grid["sheet_size"]
    bx, by = len(sheets[0]["strips"][0]), len(sheets[0]["strips"][2])  # bins along a horizontal / vertical edge
    nx, ny = max(2, round(sw / args.node) + 1), max(2, round(sh / args.node) + 1)
    lut = {(c, r): i for i, (c, r) in enumerate(zip(grid["cols"].tolist(), grid["rows"].tolist()))}
    hx, hy = max(1, round(args.window / (sw / bx))), max(1, round(args.window / (sh / by)))
    length = 2 * max(hx, hy) + 1

    def window(profile, centre, half, fill):
        out = np.full((length,) + profile.shape[1:], fill, profile.dtype)
        lo, hi = max(0, centre - half), min(len(profile), centre + half + 1)
        out[:hi - lo] = profile[lo:hi]
        return out

    node_i, node_j, wx, wy, inv = [], [], [], [], []

    def add(i, j, side_i, side_j, count, nodes_i, nodes_j, bins, half):
        si, sj = sheets[i], sheets[j]
        both = si["flags"][side_i] & sj["flags"][side_j]
        for k in range(count):
            centre = min(bins - 1, round(k / (count - 1) * bins))
            node_i.append(nodes_i(k)); node_j.append(nodes_j(k))
            wx.append(window(si["strips"][side_i], centre, half, np.nan))
            wy.append(window(sj["strips"][side_j], centre, half, np.nan))
            inv.append(window(both, centre, half, False))

    for i in range(n):
        c, r = int(grid["cols"][i]), int(grid["rows"][i])
        right, below = lut.get((c + 1, r)), lut.get((c, r + 1))
        if right is not None:  # i's right strip against j's left strip, nodes down the column
            add(i, right, 3, 2, ny, lambda k: (i * ny + k) * nx + nx - 1, lambda k: (right * ny + k) * nx, by, hy)
        if below is not None:  # i's bottom strip against j's top strip, nodes along the row
            add(i, below, 1, 0, nx, lambda k: (i * ny + ny - 1) * nx + k, lambda k: (below * ny) * nx + k, bx, hx)
    if not node_i:
        raise BalanceError("No two inputs share an edge")
    node_i, node_j = np.array(node_i), np.array(node_j)
    wx, wy, inv = np.stack(wx), np.stack(wy), np.stack(inv)
    every, only = np.zeros((len(node_i), 3, 5)), np.zeros((len(node_i), 3, 5))
    every_ok, only_ok = np.ones(len(node_i), bool), np.ones(len(node_i), bool)
    for s0 in range(0, len(node_i), 16384):
        s = slice(s0, s0 + 16384)
        for c in range(3):
            x, y = wx[s, :, c].astype(np.float64), wy[s, :, c].astype(np.float64)
            a, _, tex, inl, valid, mx, my = robust_affine(x, y, max_gain=args.max_gain)
            every[s, c] = np.c_[a, tex, inl, mx, my]
            every_ok[s] &= valid
            a, _, tex, inl, valid, mx, my = robust_affine(np.where(inv[s], x, np.nan), np.where(inv[s], y, np.nan),
                                                          max_gain=args.max_gain)
            only[s, c] = np.c_[a, tex, inl, mx, my]
            only_ok[s] &= valid
    clean = every_ok & (every[:, :, 2].min(1) > INSIDE_INLIERS)
    inside = clean & (np.abs(every[:, :, 4] - every[:, :, 3]).max(1) < INSIDE_STEP) \
        & (np.abs(np.log(every[:, :, 0])).max(1) < INSIDE_GAIN)
    use_only = ~inside & only_ok  # between flights: invariant land decides
    # no invariant land on an edge between flights: all bins if they agree, but weakly, since a
    # field harvested in one flight and not the other looks to the fit just like haze
    guessed = ~inside & ~use_only & clean
    use = inside | use_only | guessed
    fits = np.where(use_only[:, None, None], only, every)
    # a gain needs dark and bright ground in the window: a window of forest only (or roofs only)
    # gives a slope of noise, so its gain equation counts by the spread of the window
    with warnings.catch_warnings():  # windows without a valid bin
        warnings.simplefilter("ignore", RuntimeWarning)
        used = np.where(use_only[:, None, None] & ~inv[..., None], np.nan, wx)
        spread = np.nanpercentile(used, 90, axis=1) - np.nanpercentile(used, 10, axis=1)
    fits[:, :, 1] *= np.clip(np.nan_to_num(spread) / FULL_RANGE, 0, 1)
    weight = np.where(guessed, GUESSED_SEAM, 1.0)
    return node_i[use], node_j[use], fits[use], wx[use], wy[use], inv[use], inside[use], weight[use], (nx, ny)


def robust_affine(x, y, iters=6, max_gain=1.5):
    """Per row of x, y (B, L) with NaN gaps: y ~ alpha x + beta, orthogonal regression with Tukey weights.

    Returns alpha, beta, texture weight (flat windows say little about contrast), inlier share,
    a validity mask and the weighted means of x and y (where the offsets are matched).
    """
    import warnings

    import numpy as np

    ok = ~np.isnan(x) & ~np.isnan(y)
    x, y = np.where(ok, x, 0.0), np.where(ok, y, 0.0)
    count = ok.sum(1)
    # start from the plain orthogonal fit of all bins: starting from the identity, a window of two
    # clusters (dark forest, bright roofs) that haze moves in opposite directions locks onto one of them
    w = ok.astype(np.float64)
    for step in range(iters + 1):
        sw = np.maximum(w.sum(1), 1e-9)
        mx, my = (w * x).sum(1) / sw, (w * y).sum(1) / sw
        dx, dy = x - mx[:, None], y - my[:, None]
        cxx, cyy, cxy = (w * dx * dx).sum(1) / sw, (w * dy * dy).sum(1) / sw, (w * dx * dy).sum(1) / sw
        with np.errstate(invalid="ignore", divide="ignore"):
            major = (cyy - cxx + np.sqrt((cyy - cxx) ** 2 + 4 * cxy ** 2)) / (2 * cxy)
        alpha = np.where((cxx < 1) | (cxy <= 0) | ~np.isfinite(major), 1.0, major)
        alpha = np.clip(alpha, 1 / max_gain, max_gain)
        beta = my - alpha * mx
        if step == iters:
            break
        r = np.where(ok, y - (alpha[:, None] * x + beta[:, None]), np.nan)
        with warnings.catch_warnings():  # windows without valid bins: NaN here, dropped as invalid below
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(r, axis=1)
            s = 1.4826 * np.nanmedian(np.abs(r - med[:, None]), axis=1) + 0.5
        u = np.nan_to_num(r / (4.685 * s[:, None]), nan=2.0)
        w = np.where(np.abs(u) < 1, (1 - u ** 2) ** 2, 0.0)
    texture = np.minimum(cxx, cyy) / (np.minimum(cxx, cyy) + 25.0)
    inliers = (w > 0).sum(1) / np.maximum(count, 1)
    valid = (count >= MIN_BINS) & ((w > 0).sum(1) >= MIN_BINS // 2)
    return alpha, beta, texture, inliers, valid, mx, my


# ---------------------------------------------------------------- the membrane

def least_squares(rows, cols, vals, rhs, unknowns, x0):
    """min |A x - b| for a sparse A given as (rows, cols, vals): Jacobi-preconditioned CG on the normal equations."""
    import numpy as np

    b = np.asarray(rhs, np.float64)
    m = len(b)

    def A(x):
        return np.bincount(rows, weights=vals * x[cols], minlength=m)

    def At(y):
        return np.bincount(cols, weights=vals * y[rows], minlength=unknowns)

    diag = np.maximum(np.bincount(cols, weights=vals * vals, minlength=unknowns), 1e-12)
    x = x0.copy()
    r = At(b - A(x))
    z = r / diag
    p = z.copy()
    rz = r @ z
    atb = At(b)
    stop = 1e-8 * math.sqrt(atb @ atb or 1.0)
    for _ in range(20000):
        if math.sqrt(r @ r) <= stop:
            break
        q = At(A(p))
        alpha = rz / (p @ q)
        x += alpha * p
        r -= alpha * q
        z = r / diag
        rz, rz_old = r @ z, rz
        p = z + (rz / rz_old) * p
    return x




def solve(sheets, grid, args, started):
    """Node grids (n, NY, NX, 3, 2: log gain, offset) of all sheets and statistics."""
    import time
    import warnings

    import numpy as np

    from tile_mbtiles import log

    n = len(sheets)
    quantiles = np.stack([s["quantiles"] for s in sheets])
    gain, offset, gain_weight, anchored, target = anchors(quantiles)
    log(args, "balance_anchors", sheets=n, anchored=int(anchored.sum()), both_classes=int((gain_weight == 1).sum()))
    node_i, node_j, fits, wx, wy, inv, inside, weight, (nx, ny) = seams(sheets, grid, args)
    log(args, "balance_seams", nodes=len(node_i), inside_flight=int(inside.sum()),
        seconds=round(time.monotonic() - started))

    unknowns = n * ny * nx
    base = (np.arange(n) * ny * nx)[:, None]
    h_a = (base + (np.arange(ny)[:, None] * nx + np.arange(nx - 1)[None, :]).ravel()[None, :]).ravel()
    v_a = (base + (np.arange(ny - 1)[:, None] * nx + np.arange(nx)[None, :]).ravel()[None, :]).ravel()
    smooth_a, smooth_b = np.r_[h_a, v_a], np.r_[h_a + 1, v_a + nx]
    ks, ke = len(smooth_a), len(node_i)
    rows = np.r_[np.arange(ks), np.arange(ks), ks + np.arange(unknowns), ks + unknowns + np.arange(ke),
                 ks + unknowns + np.arange(ke)]
    cols = np.r_[smooth_a, smooth_b, np.arange(unknowns), node_i, node_j]
    sheet_of = np.repeat(np.arange(n), ny * nx)

    def membrane(values, weights, data, data_weight):
        ws = args.seam_weight * weights
        d = data_weight + CONDITIONING
        vals = np.r_[np.ones(ks), -np.ones(ks), d, ws, -ws]
        return least_squares(rows, cols, vals, np.r_[np.zeros(ks), d * data, ws * values], unknowns, data.copy())

    has = ~np.isnan(quantiles[:, :, 0, 0])
    nodes = np.zeros((n, ny, nx, 3, 2), np.float32)
    for c in range(3):
        alpha, tex, inl, mx, my = (fits[:, c, k] for k in range(5))
        g = membrane(np.log(alpha), weight * tex * inl, np.log(gain[sheet_of, c]),
                     args.anchor_weight * gain_weight[sheet_of])
        a = np.exp(g)
        # offsets with the gains actually solved: the corrected percentiles meet the target, and the
        # corrected means of the ground on both sides of a seam agree, a_i mx + b_i = a_j my + b_j
        mean_gain = a.reshape(n, -1).mean(1)
        data = np.zeros(n)
        for i in np.flatnonzero(anchored):
            data[i] = np.median(target[has[i], :, c].ravel() - mean_gain[i] * quantiles[i, has[i], :, c].ravel())
        off = membrane(a[node_j] * my - a[node_i] * mx, weight * inl, data[sheet_of],
                       args.anchor_weight * anchored[sheet_of])
        nodes[..., c, 0], nodes[..., c, 1] = g.reshape(n, ny, nx), off.reshape(n, ny, nx)
        log(args, "balance_solve", channel="RGB"[c], seconds=round(time.monotonic() - started))

    flat = nodes.reshape(-1, 3, 2)
    ga, oa = flat[:, :, 0], flat[:, :, 1]

    def steps(x, y, gi, oi, gj, oj):  # median step across the seam per node, in chunks (the windows are large)
        out = np.empty(ke)
        for s0 in range(0, ke, 16384):
            s = slice(s0, s0 + 16384)
            p = x[s] * np.exp(gi[s])[:, None, :] + oi[s][:, None, :]
            q = y[s] * np.exp(gj[s])[:, None, :] + oj[s][:, None, :]
            with warnings.catch_warnings():  # windows without a valid bin
                warnings.simplefilter("ignore", RuntimeWarning)
                out[s] = np.linalg.norm(np.nanmedian(p - q, axis=1), axis=1)
        return out

    zero = np.zeros((ke, 3), np.float32)
    stats = {}
    for name, x, y in (("seam", wx, wy), ("seam_invariant", np.where(inv[..., None], wx, np.nan), wy)):
        for when, s in (("before", steps(x, y, zero, zero, zero, zero)),
                        ("after", steps(x, y, ga[node_i], oa[node_i], ga[node_j], oa[node_j]))):
            for q in (50, 90, 99):
                stats[f"{name}_{when}_p{q}"] = round(float(np.nanpercentile(s, q)), 2)
    median = len(QUANTILES) // 2
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        first = quantiles[:, 0, median]  # median of the first class (forest) per sheet
        corrected = first * np.exp(nodes[..., 0].mean((1, 2))) + nodes[..., 1].mean((1, 2))
        stats["class0_spread_before"] = [round(float(v), 1) for v in np.nanstd(first, 0)]
        stats["class0_spread_after"] = [round(float(v), 1) for v in np.nanstd(corrected, 0)]
    stats["target_median"] = [[round(float(v), 1) for v in t] for t in target[:, median]]
    return nodes, stats, ke


def measure(args):
    """`mbmerge balance`: read the sheets, anchor them, fit the seams, solve, write the file."""
    import time

    import numpy as np
    from osgeo import gdal

    from tile_mbtiles import collect, log

    gdal.UseExceptions()
    started = time.monotonic()
    files = collect(args)
    grid = sheet_grid(files, args.s_srs, args.threads)
    nodes, stats, seam_nodes = solve(read_sheets(files, grid, args, started), grid, args, started)
    sw, sh = grid["sheet_size"]
    meta = {"version": BALANCE_VERSION, "origin": list(grid["origin"]), "sheet_size": [sw, sh], "srs": grid["srs"],
            "sheets": len(files), "node": args.node, "window": args.window, "sample": args.sample,
            "class_sample": args.class_sample, "classes": list(args.classes), "landcover": Path(args.landcover).name,
            "anchor_weight": args.anchor_weight, "seam_weight": args.seam_weight, "stats": stats}
    with open(args.output, "wb") as f:
        np.savez_compressed(f, nodes=nodes, cols=grid["cols"], rows=grid["rows"],
                            inputs=np.array([str(p) for p in files]), meta=np.array(json.dumps(meta)))
    log(args, "balance", output=args.output, inputs=len(files), seam_nodes=seam_nodes,
        seconds=round(time.monotonic() - started), **{k: v for k, v in stats.items() if k.startswith("seam")})
    return meta


def run(args):
    from tile_mbtiles import TileError

    try:
        args.classes = [int(v) for v in args.classes.split(",")]
    except ValueError:
        raise TileError(f"--classes must be comma-separated integers, not {args.classes!r}") from None
    if not args.landcover or not Path(args.landcover).exists():
        raise TileError(f"--landcover {args.landcover!r} does not exist")
    try:
        measure(args)
    except BalanceError as exc:
        raise TileError(str(exc)) from exc


def add_parser(sub):
    b = sub.add_parser("balance", help="one exposure and colour for a mosaic of sheets (for tile --balance)")
    b.add_argument("inputs", nargs="*", help="source rasters: 8-bit RGB sheets tiling a regular grid")
    b.add_argument("--input-dir")
    b.add_argument("--pattern", default="*.tif", help="glob within input-dir; ** supported")
    b.add_argument("--s-srs", metavar="SRS", help="coordinate system of sources without one (world files)")
    b.add_argument("--output", required=True, help="balance file to write (.npz)")
    b.add_argument("--landcover", required=True, metavar="RASTER",
                   help="land cover classes over the whole mosaic (any GDAL raster, e.g. a VRT of ESA WorldCover)")
    b.add_argument("--classes", default="10,50",
                   help="land cover classes that look the same on any summer day, darkest first "
                        "(default 10,50: WorldCover tree cover and built-up)")
    b.add_argument("--sample", type=float, default=1.0,
                   help="SRS units per pixel at which the sheets are read for their edge strips (default 1)")
    b.add_argument("--class-sample", type=float, default=10.0,
                   help="SRS units per pixel of the class percentiles, about the land cover resolution (default 10)")
    b.add_argument("--node", type=float, default=250.0, help="spacing of the correction nodes in SRS units (default 250)")
    b.add_argument("--window", type=float, default=500.0,
                   help="half-length of the strip compared at each seam node, SRS units (default 500)")
    b.add_argument("--anchor-weight", type=float, default=0.2,
                   help="pull of every node towards its sheet's anchor (default 0.2)")
    b.add_argument("--seam-weight", type=float, default=20.0, help="weight of the seam equations (default 20)")
    b.add_argument("--max-gain", type=float, default=1.5, help="limit of a gain across one seam (default 1.5)")
    b.add_argument("--threads", type=int, default=8, help="inputs read in parallel (default 8)")
    b.add_argument("--json-log", action="store_true")
    b.set_defaults(func=run)
    return b
