# SPDX-License-Identifier: MIT
"""`mbmerge tile`: GDAL rasters -> lossless flat MBTiles accepted by merge.

Needs GDAL >= 3.11 command-line tools and its Python bindings; skipped elsewhere.
Rasters are tiny EPSG:3857 GeoTIFFs aligned to a 64 px tile grid, so every
expected pixel can be stated exactly: aligned data is not resampled.
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
    """GDAL >= 3.11 command-line tools and the same version of its Python bindings."""
    if not shutil.which("gdal"):
        return False
    out = subprocess.run(["gdal", "--version"], capture_output=True, text=True).stdout
    try:
        major, minor = map(int, out.split()[1].split(".")[:2])
        from osgeo import gdal  # noqa: F401
        import numpy  # noqa: F401
    except (ImportError, IndexError, ValueError):
        return False
    return (major, minor) >= (3, 11)


def tms(col, row):
    """XYZ (col, row) at Z -> stored TMS key."""
    return (Z, col, 2 ** Z - 1 - row)


@unittest.skipUnless(have_gdal(), "GDAL >= 3.11 command-line tools and Python bindings required")
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

    def test_pyramid_built_in_memory_equals_reduction_of_stored_children(self):
        # 8x8 tiles, one worker: the workers build Z-1 and Z-2 from pixels in memory and Z-3
        # comes from stored tiles; every level must be the premultiplied 2x2 average of its
        # stored children, with integer keys, so the split is invisible in lossless output.
        gx = Image.linear_gradient("L").resize((512, 512))
        im = Image.merge("RGB", (gx, gx.rotate(90), Image.effect_noise((512, 512), 60)))
        mask = Image.new("L", (512, 512), 0)
        ImageDraw.Draw(mask).ellipse((20, 40, 470, 500), fill=255)
        im.putalpha(mask.resize((128, 128)).resize((512, 512), Image.BILINEAR))  # soft edge: partial alpha
        out, proc = self.tile(self.geotiff(im), extra=["--min-zoom", Z - 3, "--threads", "1"])
        self.assertIn('"levels": 2', proc.stderr)  # Z-1 and Z-2 really come from the workers
        with sqlite3.connect(out) as db:
            kinds = set(db.execute("SELECT DISTINCT typeof(tile_column), typeof(tile_row) FROM tiles"))
        self.assertEqual(kinds, {("integer", "integer")})
        tiles = all_tiles(out)
        for z in (Z - 1, Z - 2, Z - 3):
            children = {k for k in tiles if k[0] == z + 1}
            self.assertEqual({k for k in tiles if k[0] == z}, {(z, x // 2, y // 2) for _, x, y in children})
            for _, x, y in [k for k in tiles if k[0] == z]:
                canvas = Image.new("RGBA", (2 * TS, 2 * TS))
                for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    child = tiles.get((z + 1, 2 * x + dx, 2 * y + dy))
                    if child is not None:
                        canvas.paste(rgba(child), (dx * TS, (1 - dy) * TS))
                expected = canvas.convert("RGBa").reduce(2).convert("RGBA")
                self.assertEqual(rgba(tiles[(z, x, y)]).tobytes(), expected.tobytes(), (z, x, y))

    def test_quality_writes_lossy_webp_at_every_level(self):
        im = Image.new("RGB", (256, 256), (40, 120, 200))
        out, _ = self.tile(self.geotiff(im), extra=["--min-zoom", Z - 2, "--quality", "85"])
        tiles = all_tiles(out)
        self.assertTrue(any(k[0] == Z - 2 for k in tiles))
        self.assertTrue(all(b[12:16] == b"VP8X" or b[12:16] == b"VP8 " for b in tiles.values()))
        r, g, b, a = self.pixel(tiles, COL, ROW, 10, 10)
        self.assertEqual(a, 255)
        self.assertLessEqual(max(abs(r - 40), abs(g - 120), abs(b - 200)), 6)
        with sqlite3.connect(out) as db:
            settings = json.loads(db.execute("SELECT value FROM metadata WHERE name='mbmerge:tile'").fetchone()[0])
        self.assertEqual((settings["lossless"], settings["quality"]), (False, 85))
        _, proc = self.tile(self.geotiff(im, "png.tif"), extra=["--format", "png", "--quality", "85"], check=False)
        self.assertEqual(proc.returncode, 1)

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

    def test_cutline_larger_than_input_is_limited_to_the_input(self):
        """A country-wide cutline over one sheet: same tiles, and the warp only spans the sheet."""
        im = Image.new("RGB", (256, 256), (30, 200, 30))
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        far = 10000 * TS * RES  # thousands of tiles beyond the raster on every side
        poly = [[ulx - far, uly + far], [ulx + far, uly + far], [ulx + far, uly - far], [ulx - far, uly - far],
                [ulx - far, uly + far]]
        cut = self.root / "big.geojson"
        cut.write_text(json.dumps({"type": "FeatureCollection",
                                   "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                                   "features": [{"type": "Feature", "properties": {},
                                                 "geometry": {"type": "Polygon", "coordinates": [poly]}}]}))
        plain, _ = self.tile(self.geotiff(im))
        clipped, _ = self.tile(self.geotiff(im), output=self.root / "big.mbtiles", extra=["--cutline", cut])
        self.assertEqual(all_tiles(clipped), all_tiles(plain))
        bounds = [[float(v) for v in sqlite3.connect(p).execute(
            "SELECT value FROM metadata WHERE name = 'bounds'").fetchone()[0].split(",")] for p in (plain, clipped)]
        for a, b in zip(*bounds):  # the warp spans the raster (± a snapped pixel), not ~10000 tiles of the cutline
            self.assertAlmostEqual(a, b, delta=0.01)

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

    def test_world_file_without_srs_is_refused_unless_s_srs_is_given(self):
        im = Image.new("RGB", (128, 128), (200, 60, 10))
        jpg = self.root / "sheet.jpg"
        im.save(jpg, quality=100)
        # world file only: pixel size, rotation, rotation, -pixel size, centre of the top-left pixel
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        (self.root / "sheet.jgw").write_text(f"{RES!r}\n0\n0\n{-RES!r}\n{ulx + RES / 2!r}\n{uly - RES / 2!r}\n")
        _, proc = self.tile(jpg, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--s-srs", proc.stderr)  # not silently taken as EPSG:3857 metres
        out, _ = self.tile(jpg, extra=["--s-srs", "EPSG:3857"])
        self.assertEqual({k for k in all_tiles(out)}, {tms(COL + i, ROW + j) for i in range(2) for j in range(2)})

    def test_sheets_with_world_files_form_one_mosaic_with_the_given_srs(self):
        """Many sheets needing only --s-srs take the in-process path; a declared SRS is never overridden."""
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        sheets = []
        for i, color in enumerate(((200, 60, 10), (10, 60, 200))):
            png = self.root / f"sheet{i}.png"
            Image.new("RGB", (128, 128), color).save(png)
            x0 = ulx + i * 128 * RES  # side by side
            (self.root / f"sheet{i}.pgw").write_text(f"{RES!r}\n0\n0\n{-RES!r}\n{x0 + RES / 2!r}\n{uly - RES / 2!r}\n")
            sheets.append(png)
        _, proc = self.tile(*sheets, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--s-srs", proc.stderr)
        out, proc = self.tile(*sheets, extra=["--s-srs", "EPSG:3857"])
        self.assertNotIn("gdal_translate", proc.stderr)  # no per-sheet process
        tiles = all_tiles(out)
        self.assertEqual(set(tiles), {tms(COL + i, ROW + j) for i in range(4) for j in range(2)})
        self.assertEqual(self.pixel(tiles, COL, ROW, 5, 5), (200, 60, 10, 255))
        self.assertEqual(self.pixel(tiles, COL + 3, ROW + 1, 60, 60), (10, 60, 200, 255))
        declared = self.geotiff(Image.new("RGB", (128, 128), (0, 200, 0)), name="declared.tif")
        mixed, _ = self.tile(sheets[0], declared, output=self.root / "mixed.mbtiles", extra=["--s-srs", "EPSG:3857"])
        self.assertEqual(self.pixel(all_tiles(mixed), COL, ROW, 5, 5), (0, 200, 0, 255))  # later input on top

    def test_coarse_zoom_reads_inputs_through_their_overviews_in_place(self):
        """Sheets of two resolutions get no implicit VRT overviews; a zoom 8x coarser is read through each
        JPEG's own overviews (read_factor) and every sheet stays where it belongs."""
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        fine = RES / 8  # 1024 px per sheet: JPEG overviews 1/2 .. 1/8
        sheets = []
        for i, (size, res) in enumerate(((1024, fine), (512, fine * 2))):  # same extent, different resolution
            im = Image.new("RGB", (size, size))
            half = size // 2
            for k, color in enumerate(((220, 30, 30), (30, 220, 30), (30, 30, 220), (220, 220, 30))):
                im.paste(color, ((k % 2) * half, (k // 2) * half, (k % 2 + 1) * half, (k // 2 + 1) * half))
            jpg = self.root / f"sheet{i}.jpg"
            im.save(jpg, quality=95)
            x0 = ulx + i * 1024 * fine
            (self.root / f"sheet{i}.jgw").write_text(f"{res!r}\n0\n0\n{-res!r}\n{x0 + res / 2!r}\n{uly - res / 2!r}\n")
            sheets.append(jpg)
        out, _ = self.tile(*sheets, extra=["--s-srs", "EPSG:3857"])  # base zoom Z: 8x coarser than the sheets
        meta = dict(sqlite3.connect(out).execute("SELECT name, value FROM metadata"))
        self.assertEqual(json.loads(meta["mbmerge:tile"])["read_factor"], 8)
        tiles = all_tiles(out)
        for i in range(2):  # each sheet is 128 px at Z (two tiles of TS = 64): quadrant centres
            for k, color in enumerate(((220, 30, 30), (30, 220, 30), (30, 30, 220), (220, 220, 30))):
                x, y = i * 128 + (k % 2) * 64 + 32, (k // 2) * 64 + 32
                got = self.pixel(tiles, COL + x // TS, ROW + y // TS, x % TS, y % TS)
                self.assertLess(sum(abs(a - b) for a, b in zip(got[:3], color)), 20, (i, k, got))

    def test_jpeg_with_broken_exif_still_tiles(self):
        """Some national orthophoto sheets carry an EXIF block pointing past the file: GDAL reports an error,
        yet the pixels read fine. Neither the header check nor the workers may fail on it."""
        import struct
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        sheets = []
        for i, color in enumerate(((200, 60, 10), (10, 60, 200))):
            buf = __import__("io").BytesIO()
            Image.new("RGB", (128, 128), color).save(buf, "JPEG", quality=100)
            data = buf.getvalue()
            if i == 1:
                exif = b"Exif\x00\x00II*\x00" + struct.pack("<I", 375008158)
                data = data[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + data[2:]
            jpg = self.root / f"exif{i}.jpg"
            jpg.write_bytes(data)
            (self.root / f"exif{i}.jgw").write_text(
                f"{RES!r}\n0\n0\n{-RES!r}\n{ulx + i * 128 * RES + RES / 2!r}\n{uly - RES / 2!r}\n")
            sheets.append(jpg)
        out, _ = self.tile(*sheets, extra=["--s-srs", "EPSG:3857"])
        r, g, b, a = self.pixel(all_tiles(out), COL + 3, ROW + 1, 60, 60)
        self.assertEqual(a, 255)
        self.assertLess(abs(r - 10) + abs(g - 60) + abs(b - 200), 12)  # the broken sheet's own colour

    def test_rows_outside_the_cutline_are_not_read(self):
        """Rows of tiles the cutline does not touch are never decoded: the middle sheet here is unreadable and
        lies between two parts of one cutline (inside its extent), yet the run succeeds."""
        ulx, uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        sheets = []
        for i, color in enumerate(((200, 60, 10), (90, 90, 90), (10, 60, 200))):  # 128 px tall each, stacked
            png = self.root / f"s{i}.png"
            Image.new("RGB", (256, 128), color).save(png)
            if i == 1:
                data = png.read_bytes()
                png.write_bytes(data[:len(data) // 2])  # header intact, pixel data cut off: reading it fails
            (self.root / f"s{i}.pgw").write_text(
                f"{RES!r}\n0\n0\n{-RES!r}\n{ulx + RES / 2!r}\n{uly - i * 128 * RES - RES / 2!r}\n")
            sheets.append(png)

        def box(y0, y1):  # pixel rows y0..y1 from the top, full width
            top, bottom, x1 = uly - y0 * RES, uly - y1 * RES, ulx + 256 * RES
            return {"type": "Feature", "properties": {}, "geometry": {
                "type": "Polygon", "coordinates": [[[ulx, top], [x1, top], [x1, bottom], [ulx, bottom], [ulx, top]]]}}
        # first tile row of the top sheet and last of the bottom one; one tile of margin to the middle sheet,
        # because the warp kernel may read a line beyond the polygon
        cut = self.root / "two.geojson"
        cut.write_text(json.dumps({"type": "FeatureCollection",
                                   "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
                                   "features": [box(0, 64), box(320, 384)]}))
        out, _ = self.tile(*sheets, extra=["--s-srs", "EPSG:3857", "--cutline", cut])
        tiles = all_tiles(out)
        self.assertEqual(set(tiles), {tms(COL + i, ROW + j) for i in range(4) for j in (0, 5)})
        self.assertEqual(self.pixel(tiles, COL + 1, ROW, 10, 10), (200, 60, 10, 255))
        self.assertEqual(self.pixel(tiles, COL + 1, ROW + 5, 10, 10), (10, 60, 200, 255))

    def test_existing_output_needs_overwrite_and_errors_are_reported(self):
        out = self.root / "exists.mbtiles"
        out.write_bytes(b"keep")
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "tile", str(self.root / "missing.tif"),
                               "--output", str(out)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(out.read_bytes(), b"keep")

    def test_resume_after_kill_matches_an_uninterrupted_run(self):
        """SIGKILL during the base pass leaves <output>.tmp; --resume (other --threads) finishes it identically."""
        import random
        import signal
        import time
        rnd = random.Random(7)
        im = Image.new("RGB", (64 * 24, 64 * 24))
        im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(im.width * im.height)])
        src = self.geotiff(im, tiles=(24, 24))
        clean, _ = self.tile(src, output=self.root / "clean.mbtiles", extra=["--min-zoom", Z - 6, "--webp-method", 6])
        out = self.root / "resumed.mbtiles"
        tmp = out.with_name(out.name + ".tmp")
        cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "tile", str(src), "--output", str(out), "--zoom", str(Z),
               "--tile-size", str(TS), "--threads", "2", "--min-zoom", str(Z - 6), "--webp-method", "6"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                db = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                try:  # closed explicitly: an open WAL reader would block the final journal switch
                    if db.execute("SELECT count(*) FROM _mbmerge_tile_done").fetchone()[0]:
                        break
                finally:
                    db.close()
            except sqlite3.Error:
                pass
            time.sleep(0.05)
        self.assertIsNone(proc.poll(), "finished before it could be killed; enlarge the raster")
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        self.assertFalse(out.exists())
        resumed = subprocess.run(cmd[:-6] + ["--threads", "3", "--min-zoom", str(Z - 6), "--webp-method", "6",
                                             "--resume", "--json-log"], capture_output=True, text=True, timeout=120)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn('"event": "resume"', resumed.stderr)
        base = json.loads(next(line for line in resumed.stderr.splitlines() if '"event": "base"' in line))
        self.assertEqual(base["rows_done"], base["rows"])  # rows finished before the kill count towards progress
        self.assertFalse(tmp.exists())
        db = sqlite3.connect(out)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        db.close()
        self.assertEqual(tables, {"metadata", "tiles"})
        self.assertEqual(all_tiles(out), all_tiles(clean))

    def test_resume_refuses_other_settings(self):
        src = self.geotiff(Image.new("RGB", (256, 256), (10, 20, 30)))
        out = self.root / "r.mbtiles"
        tmp = out.with_name(out.name + ".tmp")
        self.tile(src, output=out)
        out.rename(tmp)  # stands in for a killed run: same file, but its state tables are gone
        _, proc = self.tile(src, output=out, extra=["--resume"], check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a resumable", proc.stderr)

    def test_lossy_levels_below_the_workers_are_built_from_lossless_pixels(self):
        """Every lossy tile is the lossy encoding of the exact lossless tile, also below PYRAMID_DEPTH."""
        import io
        import random
        rnd = random.Random(3)
        im = Image.new("RGB", (64 * 24, 64 * 24))
        im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(im.width * im.height)])
        src = self.geotiff(im, tiles=(24, 24))
        exact, _ = self.tile(src, output=self.root / "exact.mbtiles", extra=["--min-zoom", Z - 6])
        lossy, proc = self.tile(src, output=self.root / "lossy.mbtiles", extra=["--min-zoom", Z - 6, "--quality", 60])
        self.assertIn('"event": "overviews"', proc.stderr)  # some levels really come from stored tiles
        exact_tiles, lossy_tiles = all_tiles(exact), all_tiles(lossy)
        self.assertEqual(set(exact_tiles), set(lossy_tiles))
        for key, data in exact_tiles.items():
            out = io.BytesIO()
            rgba(data).save(out, "WEBP", quality=60, method=4)
            self.assertEqual(lossy_tiles[key], out.getvalue(), key)
        db = sqlite3.connect(lossy)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        db.close()
        self.assertEqual(tables, {"metadata", "tiles"})
        self.assertLessEqual(free, 4)  # only the dropped resume-state tables; the lossless source never lived here
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith("lossy")), ["lossy.mbtiles"])

    @unittest.skipUnless(__import__("importlib").util.find_spec("skimage"), "scikit-image not installed")
    def test_compare_scores_lossless_above_lossy_against_the_source(self):
        """Lossless tiles of an aligned raster equal the direct render; lossy ones score lower."""
        import random
        rnd = random.Random(5)
        im = Image.new("RGB", (256, 256))
        im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(256 * 256)])
        src = self.geotiff(im)
        exact, _ = self.tile(src, output=self.root / "exact.mbtiles")
        lossy, _ = self.tile(src, output=self.root / "lossy.mbtiles", extra=["--quality", 50])
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "compare", str(src),
                               "--candidate", f"exact={exact}", "--candidate", f"lossy={lossy}", "--zoom", str(Z),
                               "--samples", "16"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["exact"][str(Z)]["scored"], 16)
        self.assertEqual(report["exact"][str(Z)]["psnr"], 99.0)
        self.assertLess(report["lossy"][str(Z)]["psnr"], 40)


if __name__ == "__main__":
    unittest.main()
