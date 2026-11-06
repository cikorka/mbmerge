# SPDX-License-Identifier: MIT
"""`mbmerge grade` and `tile --grade`: one pixel function for the whole mosaic, applied before encoding."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from PIL import Image
from test_merge import ROOT, all_tiles, rgba
from test_tile import COL, RES, ROW, TS, WORLD, Z, have_gdal, tms

sys.path.insert(0, str(ROOT))
from grade_mbtiles import Grader, GradeError, lut, validate  # noqa: E402

BASE = {"version": 1, "black": [10, 20, 30], "white": [240, 230, 220], "gamma": 1.2, "saturation": 1.3,
        "vibrance": 0.4}


class GradeFunctionTests(unittest.TestCase):
    def test_levels_map_black_and_white_points_and_clip_outside(self):
        table = lut({**BASE, "gamma": 1.0})
        self.assertEqual([table[c][BASE["black"][c]] for c in range(3)], [0, 0, 0])
        self.assertEqual([table[c][BASE["white"][c]] for c in range(3)], [255, 255, 255])
        self.assertEqual(table[0][0], 0)
        self.assertEqual(table[0][255], 255)
        self.assertTrue(all((table[c][1:] >= table[c][:-1]).all() for c in range(3)))  # monotonic

    def test_grey_stays_grey_and_saturation_moves_away_from_luma(self):
        import numpy as np

        neutral = {**BASE, "black": [0] * 3, "white": [255] * 3, "gamma": 1.0}
        px = np.array([[[128, 128, 128, 255], [150, 120, 90, 77]]], np.uint8)
        out = Grader(neutral)(px.copy())
        self.assertEqual(out[0, 0].tolist(), [128, 128, 128, 255])
        self.assertGreater(out[0, 1, 0] - out[0, 1, 2], 150 - 90)  # more colourful
        self.assertEqual(out[0, 1, 3], 77)  # alpha untouched

    def test_vibrance_boosts_dull_colours_more_than_saturated_ones(self):
        import numpy as np

        only_vibrance = {**BASE, "black": [0] * 3, "white": [255] * 3, "gamma": 1.0, "saturation": 1.0, "vibrance": 1.0}
        dull, vivid = [140, 128, 116, 255], [230, 40, 30, 255]
        out = Grader(only_vibrance)(np.array([[dull, vivid]], np.uint8))
        spread = lambda p: int(max(p[:3])) - int(min(p[:3]))
        self.assertGreater(spread(out[0, 0]) / spread(dull), spread(out[0, 1]) / spread(vivid))

    def test_invalid_grades_are_refused(self):
        for bad in ({**BASE, "version": 2}, {**BASE, "black": [200, 0, 0], "white": [100, 255, 255]},
                    {**BASE, "gamma": 0}, {**BASE, "saturation": -1}):
            with self.assertRaises(GradeError):
                validate(bad)


@unittest.skipUnless(have_gdal(), "GDAL >= 3.11 command-line tools and Python bindings required")
class GradeCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sheet(self, name, color, col_offset, size=128):
        """A plain sheet with a world file (no SRS), placed `col_offset` tiles right of COL/ROW."""
        import random

        rnd = random.Random(name)
        im = Image.new("RGB", (size, size))
        im.putdata([tuple(max(0, min(255, c + rnd.randrange(-20, 21))) for c in color) for _ in range(size * size)])
        png = self.root / f"{name}.png"
        im.save(png)
        ulx, uly = -WORLD / 2 + (COL + col_offset) * TS * RES, WORLD / 2 - ROW * TS * RES
        (self.root / f"{name}.pgw").write_text(f"{RES!r}\n0\n0\n{-RES!r}\n{ulx + RES / 2!r}\n{uly - RES / 2!r}\n")
        return png

    def cli(self, *args):
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), *map(str, args)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def tile(self, inputs, output, *extra):
        self.cli("tile", *inputs, "--output", output, "--zoom", Z, "--tile-size", TS, "--threads", 2,
                 "--s-srs", "EPSG:3857", *extra)
        return output

    def test_measure_is_deterministic_and_stretches_a_dull_mosaic(self):
        sheets = [self.sheet("a", (110, 120, 100), 0), self.sheet("b", (120, 125, 105), 2)]
        outs = []
        for i in range(2):
            out = self.root / f"g{i}.json"
            self.cli("grade", *sheets, "--s-srs", "EPSG:3857", "--saturation", 1.2, "--vibrance", 0.3,
                     "--gamma", "auto", "--output", out)
            outs.append(json.loads(out.read_text()))
        self.assertEqual(outs[0], outs[1])
        g = outs[0]
        self.assertTrue(all(b > 60 and w < 170 for b, w in zip(g["black"], g["white"])))  # dull input: tight levels
        self.assertEqual((g["saturation"], g["vibrance"]), (1.2, 0.3))

    def test_graded_tiles_equal_ungraded_tiles_graded_afterwards(self):
        """Lossless: tile --grade == grade(tile); so neighbours that matched before still match after."""
        import numpy as np

        sheets = [self.sheet("a", (110, 120, 100), 0), self.sheet("b", (110, 120, 100), 2)]
        grade = self.root / "grade.json"
        grade.write_text(json.dumps(BASE))
        plain = all_tiles(self.tile(sheets, self.root / "plain.mbtiles"))
        graded = all_tiles(self.tile(sheets, self.root / "graded.mbtiles", "--grade", grade))
        self.assertEqual(set(plain), set(graded))
        grader = Grader(BASE)
        for key, data in plain.items():
            expected = grader(np.array(rgba(data)))
            self.assertTrue(np.array_equal(np.array(rgba(graded[key])), expected), key)
        meta = dict(sqlite3.connect(self.root / "graded.mbtiles").execute("SELECT name, value FROM metadata"))
        self.assertEqual(json.loads(meta["mbmerge:tile"])["grade"]["saturation"], BASE["saturation"])

    def test_resume_refuses_another_grade(self):
        src = [self.sheet("a", (110, 120, 100), 0)]
        g1, g2 = self.root / "g1.json", self.root / "g2.json"
        g1.write_text(json.dumps(BASE))
        g2.write_text(json.dumps({**BASE, "saturation": 1.5}))
        out = self.root / "r.mbtiles"
        tmp = out.with_name(out.name + ".tmp")
        cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "tile", *map(str, src), "--output", str(out),
               "--zoom", str(Z), "--tile-size", str(TS), "--threads", "1", "--s-srs", "EPSG:3857"]
        # a run that left its .tmp behind: start one, kill it once the state tables exist
        import signal
        import time
        proc = subprocess.Popen(cmd + ["--grade", str(g1), "--webp-method", "6"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                db = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                try:
                    if db.execute("SELECT count(*) FROM _mbmerge_tile_state").fetchone()[0]:
                        break
                finally:
                    db.close()
            except sqlite3.Error:
                pass
            time.sleep(0.02)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        if not tmp.exists():
            self.skipTest("run finished before it could be interrupted")
        refused = subprocess.run(cmd + ["--grade", str(g2), "--webp-method", "6", "--resume"],
                                 capture_output=True, text=True, timeout=120)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("other inputs or settings", refused.stderr)

    def test_preview_writes_side_by_side_crops(self):
        src = [self.sheet("a", (110, 120, 100), 0, size=256)]
        grade = self.root / "grade.json"
        grade.write_text(json.dumps(BASE))
        lon = (-WORLD / 2 + (COL + 2) * TS * RES) / WORLD * 360
        import math
        y = WORLD / 2 - (ROW + 2) * TS * RES
        lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
        out = self.root / "preview"
        self.cli("grade", *src, "--s-srs", "EPSG:3857", "--preview", grade, "--at", f"{lon},{lat}",
                 "--zoom", Z, "--size", 64, "--out", out)
        with Image.open(out / "preview_0.jpg") as im:
            self.assertEqual(im.size, (64 * 2 + 8, 64))
        self.assertIn("preview_0.jpg", (out / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
