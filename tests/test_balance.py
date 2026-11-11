# SPDX-License-Identifier: MIT
"""`mbmerge balance` and `tile --balance`: sheets anchored on invariant land, seams closed by a membrane."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from PIL import Image
from test_merge import ROOT, all_tiles, rgba
from test_tile import COL, RES, ROW, TS, WORLD, Z, have_gdal

sys.path.insert(0, str(ROOT))
from balance_mbtiles import BalanceError, Balancer, load  # noqa: E402

SIZE = 512  # px per sheet at RES (128 bins along an edge); a grid of COLS x ROWS sheets
COLS, ROWS = 4, 2


PALETTE = ((70, 110, 60), (160, 150, 110), (110, 140, 80), (60, 80, 50), (175, 165, 160))
CLASSES = (30, 40, 40, 10, 50)  # WorldCover: grassland, cropland, cropland, tree cover, built-up
FOREST, FIELDS = 10, (30, 40)


def terrain(width, height, seed=7):
    """A landscape-like RGB image (patches of fields, forest and villages) and its land cover classes."""
    import numpy as np

    rnd = np.random.default_rng(seed)
    palette = np.array(PALETTE, float)
    count = 1600
    centres = rnd.uniform((0, 0), (width, height), (count, 2))
    radii = rnd.uniform(8, 40, count)
    kinds = rnd.integers(0, len(palette), count)
    colours = palette[kinds]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    best, nearest = np.full((height, width), np.inf, np.float32), np.zeros((height, width), int)
    for k in range(count):  # nearest blob, one at a time (bounded memory)
        d = ((xx - centres[k, 0]) ** 2 + (yy - centres[k, 1]) ** 2) / radii[k] ** 2
        closer = d < best
        best[closer], nearest[closer] = d[closer], k
    img = colours[nearest] + rnd.integers(-8, 9, (height, width, 1))
    return np.clip(img, 0, 255), np.array(CLASSES, np.uint8)[kinds[nearest]]


def haze(img):
    """Another flight: less contrast, brighter and bluer."""
    import numpy as np

    return np.clip(img * 0.75 + np.array([30, 28, 40]), 0, 255)


def harvest(img, classes):
    """Another flight later in the summer: the fields are harvested, forests and villages unchanged."""
    import numpy as np

    out = img.copy()
    fields = np.isin(classes, FIELDS)
    out[fields] = np.clip(img[fields] * 0.4 + np.array([125, 110, 75]), 0, 255)
    return out


@unittest.skipUnless(have_gdal(), "GDAL >= 3.11 command-line tools and Python bindings required")
class BalancerTests(unittest.TestCase):
    def field(self, path, nodes, cols, rows, sheet=(1000.0, 800.0)):
        """A balance file over a grid of sheets in EPSG:3857 with origin (0, rows * height)."""
        import numpy as np
        from osgeo import osr

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3857)
        meta = {"version": 2, "origin": [0.0, rows * sheet[1]], "sheet_size": list(sheet), "srs": srs.ExportToWkt(),
                "sheets": cols * rows, "node": 250, "classes": [10, 50]}
        c, r = np.meshgrid(np.arange(cols), np.arange(rows))
        np.savez(path, nodes=nodes.astype(np.float32), cols=c.ravel(), rows=r.ravel(),
                 inputs=np.array(["x"] * (cols * rows)), meta=np.array(json.dumps(meta)))
        return path

    def test_result_depends_on_position_only_not_on_how_the_image_is_split(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            rnd = np.random.default_rng(1)
            nodes = np.stack([rnd.uniform(-0.2, 0.2, (4, 5, 6, 3)), rnd.uniform(-20, 20, (4, 5, 6, 3))], -1)
            b = Balancer(self.field(Path(tmp) / "f.npz", nodes, 2, 2))
            img = np.dstack([rnd.integers(0, 256, (80, 100, 3)), np.full((80, 100), 255)]).astype(np.uint8)
            whole = b(img.copy(), 0, 1600, 20)
            parts = img.copy()
            for r0, r1 in ((0, 7), (7, 41), (41, 80)):  # rows of tiles of different heights, as the workers read
                for c0, c1 in ((0, 37), (37, 100)):
                    b(parts[r0:r1, c0:c1], c0 * 20, 1600 - r0 * 20, 20)
            self.assertTrue(np.array_equal(whole, parts))
            columns, rows = img.copy(), img.copy()  # one pixel wide or high: every offset to the lattice occurs
            for c0 in range(100):
                b(columns[:, c0:c0 + 1], c0 * 20, 1600, 20)
            for r0 in range(80):
                b(rows[r0:r0 + 1], 0, 1600 - r0 * 20, 20)
            self.assertTrue(np.array_equal(whole, columns))
            self.assertTrue(np.array_equal(whole, rows))

    def test_each_pixel_takes_the_correction_of_its_own_sheet(self):
        """Constant offsets per sheet: the boundary falls exactly between the sheets, transparent stays."""
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            nodes = np.zeros((2, 3, 3, 3, 2))
            nodes[0, ..., 1], nodes[1, ..., 1] = 10, -10
            b = Balancer(self.field(Path(tmp) / "f.npz", nodes, 2, 1, sheet=(100.0, 100.0)))
            img = np.full((10, 20, 4), 100, np.uint8)
            img[0, 0, 3] = 0
            out = b(img.copy(), 0, 100, 10)  # 20 px of 10 m: sheet 0 then sheet 1
            self.assertTrue((out[1:, :10, 0] == 110).all() and (out[:, 10:, 0] == 90).all())
            self.assertEqual(out[0, 0].tolist(), [100, 100, 100, 0])
            self.assertTrue((b(img.copy(), 500, 100, 10)[..., :3] == 100).all())  # outside the grid: untouched

    def test_files_that_are_not_a_balance_file_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "x.npz"
            other.write_bytes(b"not a zip")
            with self.assertRaises(BalanceError):
                load(other)


@unittest.skipUnless(have_gdal(), "GDAL >= 3.11 command-line tools and Python bindings required")
class BalanceCommandTests(unittest.TestCase):
    """A grid of 4 x 2 sheets of one landscape; the right half was flown on another day."""

    @classmethod
    def setUpClass(cls):
        cls.land, cls.classes = terrain(COLS * SIZE, ROWS * SIZE)

    def setUp(self):
        from osgeo import gdal

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ulx, self.uly = -WORLD / 2 + COL * TS * RES, WORLD / 2 - ROW * TS * RES
        self.landcover = self.root / "landcover.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(self.landcover), COLS * SIZE, ROWS * SIZE, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((self.ulx, RES, 0, self.uly, 0, -RES))
        ds.SetProjection("EPSG:3857")
        ds.GetRasterBand(1).WriteArray(self.classes)
        ds = None
        self.flight(haze)

    def flight(self, change):
        """Write the sheets; the right half changed by `change(image, classes)` or `change(image)`."""
        self.sheets = []
        for r in range(ROWS):
            for c in range(COLS):
                part = self.land[r * SIZE:(r + 1) * SIZE, c * SIZE:(c + 1) * SIZE]
                if c >= COLS // 2:
                    cls = self.classes[r * SIZE:(r + 1) * SIZE, c * SIZE:(c + 1) * SIZE]
                    part = change(part, cls) if change is harvest else change(part)
                png = self.root / f"s{c}{r}.png"
                Image.fromarray(part.astype("uint8")).save(png)
                x0, y0 = self.ulx + c * SIZE * RES, self.uly - r * SIZE * RES
                png.with_suffix(".pgw").write_text(f"{RES!r}\n0\n0\n{-RES!r}\n{x0 + RES / 2!r}\n{y0 - RES / 2!r}\n")
                self.sheets.append(png)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), *map(str, args)],
                              capture_output=True, text=True, timeout=300)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def balance(self, *extra, sheets=None):
        out = self.root / "balance.npz"
        self.cli("balance", *(sheets or self.sheets), "--s-srs", "EPSG:3857", "--landcover", self.landcover,
                 "--sample", RES, "--class-sample", 4 * RES, "--node", 128 * RES, "--window", 256 * RES,
                 "--threads", 2, "--output", out, *extra)
        return out

    def tile(self, output, *extra):
        self.cli("tile", *self.sheets, "--output", output, "--zoom", Z, "--tile-size", TS, "--threads", 2,
                 "--s-srs", "EPSG:3857", *extra)
        return output

    def canvas(self, mbtiles):
        import numpy as np

        cols, rows = COLS * SIZE // TS, ROWS * SIZE // TS
        img = np.zeros((rows * TS, cols * TS, 4), np.uint8)
        for (z, x, y), data in all_tiles(mbtiles).items():
            col, row = x - COL, (2 ** Z - 1 - y) - ROW
            if 0 <= col < cols and 0 <= row < rows:
                img[row * TS:(row + 1) * TS, col * TS:(col + 1) * TS] = np.array(rgba(data))
        return img[..., :3].astype(float)

    def step(self, img, x):
        """Colour step across the vertical line x beyond what the landscape itself has there."""
        import numpy as np

        seen = img[:, x - 2:x].mean((0, 1)) - img[:, x:x + 2].mean((0, 1))
        true = self.land[:, x - 2:x].mean((0, 1)) - self.land[:, x:x + 2].mean((0, 1))
        return np.abs(seen - true).max()

    def forest(self, img, half):
        """Mean colour of the forest in the left (0) or right (1) half."""
        import numpy as np

        cols = slice(0, COLS // 2 * SIZE) if half == 0 else slice(COLS // 2 * SIZE, None)
        return img[:, cols][self.classes[:, cols] == FOREST].mean(0)

    def test_hazy_flight_gets_the_exposure_of_the_rest_and_the_seam_closes(self):
        import numpy as np

        plain = self.canvas(self.tile(self.root / "plain.mbtiles"))
        out = self.balance()
        balanced = self.canvas(self.tile(self.root / "balanced.mbtiles", "--balance", out))
        seam = COLS // 2 * SIZE
        self.assertGreater(self.step(plain, seam), 8)  # the second flight is visibly off
        self.assertLess(self.step(balanced, seam), 3)
        self.assertGreater(np.abs(self.forest(plain, 1) - self.forest(plain, 0)).max(), 15)
        self.assertLess(np.abs(self.forest(balanced, 1) - self.forest(balanced, 0)).max(), 4)  # one exposure
        for x in (SIZE, 3 * SIZE):  # edges inside one flight stay seamless
            self.assertLess(self.step(balanced, x), self.step(plain, x) + 2, x)
        stats = load(out)["meta"]["stats"]
        self.assertLess(stats["seam_after_p99"], stats["seam_before_p99"])
        meta = dict(sqlite3.connect(self.root / "balanced.mbtiles").execute("SELECT name, value FROM metadata"))
        self.assertEqual(json.loads(meta["mbmerge:tile"])["balance"]["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())

    def test_harvested_fields_do_not_shift_the_tone_of_their_flight(self):
        """Only the fields changed between the flights: forests and villages, so the colour, stay as they are."""
        import numpy as np

        self.flight(harvest)
        plain = self.canvas(self.tile(self.root / "plain.mbtiles"))
        balanced = self.canvas(self.tile(self.root / "balanced.mbtiles", "--balance", self.balance()))
        for half in (0, 1):
            self.assertLess(np.abs(self.forest(balanced, half) - self.forest(plain, half)).max(), 3, half)
        fields = np.isin(self.classes, FIELDS)
        self.assertLess(np.abs(balanced[fields].mean(0) - plain[fields].mean(0)).max(), 3)

    def test_grade_measures_the_levelled_mosaic(self):
        """grade --balance samples what tile --balance produces: the hazy half no longer widens the levels."""
        out = self.balance()
        plain, levelled = self.root / "plain.json", self.root / "levelled.json"
        self.cli("grade", *self.sheets, "--s-srs", "EPSG:3857", "--linked-levels", "--output", plain)
        self.cli("grade", *self.sheets, "--s-srs", "EPSG:3857", "--balance", out, "--output", levelled)
        a, b = json.loads(plain.read_text()), json.loads(levelled.read_text())
        self.assertTrue(b["sample"]["linked_levels"])
        self.assertEqual(b["sample"]["balance"], out.name)
        self.assertLess(b["white"][0] - b["black"][0], a["white"][0] - a["black"][0] - 3)

    def test_inputs_off_the_grid_are_refused(self):
        moved = self.root / "moved.png"
        Image.fromarray(self.land[:SIZE, :SIZE].astype("uint8")).save(moved)
        x0 = self.ulx + (COLS + 0.5) * SIZE * RES  # half a sheet off the grid
        moved.with_suffix(".pgw").write_text(f"{RES!r}\n0\n0\n{-RES!r}\n{x0 + RES / 2!r}\n{self.uly - RES / 2!r}\n")
        proc = self.cli("balance", *self.sheets, moved, "--s-srs", "EPSG:3857", "--landcover", self.landcover,
                        "--output", self.root / "b.npz", ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("regular grid", proc.stderr)

    def test_tile_refuses_a_file_that_is_not_a_balance_file(self):
        other = self.root / "other.npz"
        other.write_text("{}")
        proc = self.cli("tile", *self.sheets, "--output", self.root / "x.mbtiles", "--zoom", Z, "--tile-size", TS,
                        "--s-srs", "EPSG:3857", "--balance", other, ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--balance", proc.stderr)


if __name__ == "__main__":
    unittest.main()
