# SPDX-License-Identifier: MIT
"""`mbmerge tile`: GDAL rasters -> lossless flat MBTiles accepted by merge.

Needs GDAL >= 3.11 command-line tools; skipped elsewhere. Rasters are tiny
EPSG:3857 GeoTIFFs aligned to a 64 px tile grid (the minimum of gdal raster tile), so every expected pixel can be
stated exactly: nearest at the base level means no resampling of aligned data.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw
from test_merge import ROOT, all_tiles, rgba

WORLD = 40075016.685578488
Z, TS = 10, 64
RES = WORLD / (TS * 2 ** Z)
COL, ROW = 512, 400  # XYZ tile of the raster's top-left corner


def have_gdal():
    if not shutil.which("gdal"):
        return False
    out = subprocess.run(["gdal", "--version"], capture_output=True, text=True).stdout
    try:
        major, minor = map(int, out.split()[1].split(".")[:2])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= (3, 11)


def tms(col, row):
    """XYZ (col, row) at Z -> stored TMS key."""
    return (Z, col, 2 ** Z - 1 - row)


@unittest.skipUnless(have_gdal(), "GDAL >= 3.11 command-line tools required")
class TileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def geotiff(self, im, name="in.tif", tiles=(4, 4)):
        """Georeference an image so pixel (0,0) is the corner of tile COL/ROW."""
        png = self.root / (name + ".png")
        im.save(png)
        out = self.root / name
        ulx = -WORLD / 2 + COL * TS * RES
        uly = WORLD / 2 - ROW * TS * RES
        subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:3857", "-a_ullr", repr(ulx), repr(uly),
                        repr(ulx + im.width * RES), repr(uly - im.height * RES), png, out], check=True)
        return out

    def tile(self, *inputs, output=None, extra=(), check=True):
        output = output or self.root / "out.mbtiles"
        cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "tile", *map(str, inputs), "--output", str(output),
               "--overwrite", "--zoom", str(Z), "--tile-size", str(TS), "--threads", "2", "--json-log", *map(str, extra)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return output, proc

    def pixel(self, tiles, col, row, x, y):
        return rgba(tiles[tms(col, row)]).getpixel((x, y))

    def test_output_is_lossless_flat_tms_and_merge_accepts_it(self):
        im = Image.new("RGB", (256, 256), (40, 120, 200))
        out, _ = self.tile(self.geotiff(im), extra=["--min-zoom", Z - 2])
        tiles = all_tiles(out)
        self.assertEqual({k for k in tiles if k[0] == Z}, {tms(COL + i, ROW + j) for i in range(4) for j in range(4)})
        self.assertTrue(any(k[0] == Z - 2 for k in tiles))  # preview overviews
        self.assertTrue(all(b[12:16] == b"VP8L" for b in tiles.values()))
        self.assertEqual(self.pixel(tiles, COL, ROW, 0, 0), (40, 120, 200, 255))
        with sqlite3.connect(out) as db:
            meta = dict(db.execute("SELECT * FROM metadata"))
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual((meta["format"], meta["scheme"], meta["maxzoom"]), ("webp", "tms", str(Z)))
        self.assertEqual(json.loads(meta["mbmerge:tile"])["zoom"], Z)
        merged = self.root / "merged.mbtiles"
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "merge", "--input", str(out),
                               "--output", str(merged), "--base-zoom", str(Z), "--tile-size", str(TS),
                               "--workers", "1"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(all_tiles(merged)[tms(COL, ROW)], tiles[tms(COL, ROW)])  # copied, not re-encoded

    def test_collar_removes_noisy_edge_but_keeps_interior_black_and_white(self):
        im = Image.new("RGB", (256, 256), (0, 0, 0))
        draw = ImageDraw.Draw(im)
        for i in range(0, 256, 2):  # JPEG-like noise inside the collar
            draw.point([(i, 1), (1, i), (254, i), (i, 254)], fill=(9, 6, 11))
        draw.rectangle((64, 64, 191, 191), fill=(128, 128, 128))
        draw.rectangle((80, 80, 95, 95), fill=(0, 0, 0))        # black roof inside
        draw.rectangle((160, 160, 175, 175), fill=(255, 255, 255))  # white area inside
        out, _ = self.tile(self.geotiff(im), extra=["--collar", "black", "--collar", "white", "--near", "15"])
        tiles = all_tiles(out)
        self.assertNotIn(tms(COL, ROW), tiles)  # tile 0,0 is collar only: empty, skipped
        self.assertEqual(self.pixel(tiles, COL + 1, ROW + 1, 16, 16), (0, 0, 0, 255))  # black roof inside
        self.assertEqual(self.pixel(tiles, COL + 2, ROW + 2, 32, 32), (255, 255, 255, 255))  # white area inside
        self.assertNotIn(tms(COL + 1, ROW), tiles)  # collar with noisy pixels only: removed entirely

    def test_nodata_color_is_transparent_everywhere(self):
        im = Image.new("RGB", (256, 256), (128, 128, 128))
        ImageDraw.Draw(im).rectangle((160, 160, 175, 175), fill=(255, 255, 255))
        kept, _ = self.tile(self.geotiff(im))
        self.assertEqual(self.pixel(all_tiles(kept), COL + 2, ROW + 2, 32, 32), (255, 255, 255, 255))
        out, _ = self.tile(self.geotiff(im), output=self.root / "nd.mbtiles", extra=["--nodata", "255,255,255"])
        self.assertEqual(self.pixel(all_tiles(out), COL + 2, ROW + 2, 32, 32)[3], 0)
        self.assertEqual(self.pixel(all_tiles(out), COL + 2, ROW + 2, 0, 0), (128, 128, 128, 255))

    def test_cutline_clips_and_drops_tiles_outside(self):
        im = Image.new("RGB", (256, 256), (200, 30, 30))
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        x1 = ulx + 128 * RES  # left half of the raster
        poly = [[ulx, uly], [x1, uly], [x1, uly - 256 * RES], [ulx, uly - 256 * RES], [ulx, uly]]
        cut = self.root / "cut.geojson"
        cut.write_text(json.dumps({"type": "FeatureCollection",
                                   "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                                   "features": [{"type": "Feature", "properties": {"KOD": "1"},
                                                 "geometry": {"type": "Polygon", "coordinates": [poly]}}]}))
        out, _ = self.tile(self.geotiff(im), extra=["--cutline", cut, "--cwhere", "KOD = '1'"])
        cols = {k[1] for k in all_tiles(out)}
        self.assertEqual(cols, {COL, COL + 1})

    def test_neighbours_clipped_by_shared_boundary_are_complementary(self):
        """Different source grids, one diagonal boundary: no overlap and no crack."""
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        w = 256 * RES
        mid_top, mid_bottom = ulx + 0.37 * w, ulx + 0.71 * w
        left = [[ulx, uly], [mid_top, uly], [mid_bottom, uly - w], [ulx, uly - w], [ulx, uly]]
        right = [[mid_top, uly], [ulx + w, uly], [ulx + w, uly - w], [mid_bottom, uly - w], [mid_top, uly]]
        cut = self.root / "obce.geojson"
        cut.write_text(json.dumps({"type": "FeatureCollection",
                                   "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                                   "features": [{"type": "Feature", "properties": {"KOD": k},
                                                 "geometry": {"type": "Polygon", "coordinates": [p]}}
                                                for k, p in (("1", left), ("2", right))]}))
        outs = []
        for kod, scale, shift, color in (("1", 0.73, 0.31, (200, 0, 0)), ("2", 1.37, 0.58, (0, 0, 200))):
            n = int(300 / scale)
            png = self.root / f"s{kod}.png"
            Image.new("RGB", (n, n), color).save(png)
            tif = self.root / f"s{kod}.tif"
            x0, y0 = ulx - (10 + shift) * RES, uly + (10 + shift) * RES  # its own, unaligned grid
            subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:3857", "-a_ullr", repr(x0), repr(y0),
                            repr(x0 + n * scale * RES), repr(y0 - n * scale * RES), png, tif], check=True)
            out, _ = self.tile(tif, output=self.root / f"o{kod}.mbtiles",
                               extra=["--cutline", cut, "--cwhere", f"KOD = '{kod}'"])
            outs.append(all_tiles(out))
        overlap = cracks = covered = 0
        for key in set(outs[0]) | set(outs[1]):
            masks = [[[rgba(o[key]).getpixel((x, y))[3] > 0 for x in range(TS)] for y in range(TS)]
                     if key in o else [[False] * TS for _ in range(TS)] for o in outs]
            for y in range(TS):
                for x in range(TS):
                    a, b = masks[0][y][x], masks[1][y][x]
                    overlap += a and b
                    covered += a or b
            for y in range(TS):  # every row of the raster crosses the boundary once
                row = [masks[0][y][x] or masks[1][y][x] for x in range(TS)]
                cracks += sum(1 for x in range(1, TS - 1) if not row[x] and row[x - 1] and row[x + 1])
        self.assertEqual(overlap, 0)
        self.assertEqual(cracks, 0)
        self.assertEqual(covered, 256 * 256)  # the two polygons tile the whole raster

    def test_binary_alpha_snaps_partial_alpha_at_base_zoom(self):
        im = Image.new("RGBA", (256, 256), (10, 10, 200, 255))
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 63, 63), fill=(10, 10, 200, 100))
        draw.rectangle((64, 0, 127, 63), fill=(10, 10, 200, 200))
        plain, _ = self.tile(self.geotiff(im))
        self.assertEqual(self.pixel(all_tiles(plain), COL, ROW, 20, 20)[3], 100)
        out, _ = self.tile(self.geotiff(im), output=self.root / "bin.mbtiles", extra=["--binary-alpha"])
        tiles = all_tiles(out)
        self.assertNotIn(tms(COL, ROW), tiles)  # alpha 100 -> 0: empty
        self.assertEqual(self.pixel(tiles, COL + 1, ROW, 20, 20)[3], 255)

    def test_later_input_is_drawn_over_earlier(self):
        a = self.geotiff(Image.new("RGB", (256, 256), (200, 0, 0)), "a.tif")
        b = self.geotiff(Image.new("RGB", (128, 128), (0, 0, 200)), "b.tif")
        out, _ = self.tile(a, b)
        tiles = all_tiles(out)
        self.assertEqual(self.pixel(tiles, COL, ROW, 0, 0), (0, 0, 200, 255))
        self.assertEqual(self.pixel(tiles, COL + 3, ROW + 3, 0, 0), (200, 0, 0, 255))

    def test_pdf_input_is_rasterized_at_requested_dpi(self):
        tif = self.geotiff(Image.new("RGB", (256, 256), (30, 160, 30)))
        pdf = self.root / "in.pdf"
        made = subprocess.run(["gdal_translate", "-q", "-of", "PDF", str(tif), str(pdf)], capture_output=True)
        if made.returncode != 0:
            self.skipTest("GDAL build cannot write PDF")
        out, _ = self.tile(pdf, extra=["--pdf-dpi", "72"])
        self.assertIn(tms(COL + 1, ROW + 1), all_tiles(out))

    def test_existing_output_needs_overwrite_and_errors_are_reported(self):
        out = self.root / "exists.mbtiles"
        out.write_bytes(b"keep")
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "tile", str(self.root / "missing.tif"),
                               "--output", str(out)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(out.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
