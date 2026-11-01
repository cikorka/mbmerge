# SPDX-License-Identifier: MIT
"""Pixel, incremental and crash-recovery contracts for the lossless pyramid.

Oracles use asymmetric colors and TMS quadrants, including inputs whose supplied
low levels are deliberately wrong. Real CLI processes exercise spawn and WAL.
"""
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

from PIL import Image
import test_merge as support
from test_merge import ROOT, SIZE, all_tiles, fixture, image, m, rgba, webp
from test_png import png


class PyramidTests(unittest.TestCase):
    setUp = support.MergeTests.setUp
    tearDown = support.MergeTests.tearDown
    pixels_equal = support.MergeTests.pixels_equal

    def cli(self, *extra, **kwargs):
        return support.MergeTests.cli(self, "--mode", "pyramid", *extra, **kwargs)

    def metadata(self, output=None):
        with sqlite3.connect(output or self.out) as db:
            return dict(db.execute("SELECT * FROM metadata"))

    def test_default_mode_and_tms_parent_quadrants_ignore_supplied_overviews(self):
        a = self.root / "a.mbtiles"
        colors = {(0, 0): (240, 0, 0, 255), (1, 0): (0, 240, 0, 255),
                  (0, 1): (0, 0, 240, 255), (1, 1): (240, 240, 240, 255)}
        fixture(a, [(3, 4 + dx, 2 + dy, png(image(color))) for (dx, dy), color in colors.items()]
                + [(0, 0, 0, b"unused invalid low-zoom image"),
                   (2, 2, 1, png(image((255, 255, 0, 255))))])
        # Do not pass --mode: this checks the new public default itself.
        result = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "merge",
                                 "--input", str(a), "--output", str(self.out),
                                 "--tile-size", str(SIZE), "--workers", "2", "--json-log"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        tiles = all_tiles(self.out)
        self.assertEqual({z for z, x, y in tiles}, {0, 1, 2, 3})
        self.assertEqual(len(tiles), 7)
        parent = rgba(tiles[(2, 2, 1)])
        for (dx, dy), color in colors.items():
            self.assertEqual(parent.getpixel((dx * 8 + 2, (1 - dy) * 8 + 2)), color)
        self.assertEqual(json.loads(result.stderr.splitlines()[-1])["overview_tiles"], 3)
        meta = self.metadata()
        self.assertEqual((meta["minzoom"], meta["maxzoom"], meta["mbmerge:phase"]), ("0", "3", "complete"))
        expected_bounds = m.union(m.tile_bounds(3, 4, 2), m.tile_bounds(3, 5, 3))
        for actual, expected in zip(map(float, meta["bounds"].split(",")), expected_bounds):
            self.assertAlmostEqual(actual, expected, places=8)  # not the footprint of the z=0 tile

    def test_merge_before_downsampling_and_premultiplied_alpha(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red, blue = image((0, 0, 0, 0)), image((0, 0, 0, 0))
        for y in range(SIZE):
            for x in range(SIZE):
                (red if x % 2 == 0 else blue).putpixel((x, y), (255, 0, 0, 255) if x % 2 == 0 else (0, 0, 255, 255))
        fixture(a, [(1, 0, 0, png(red))])
        fixture(b, [(1, 0, 0, webp(blue))])
        for fmt in ("png", "webp"):
            out = self.root / f"colors_{fmt}.mbtiles"
            self.cli("--input", a, "--input", b, "--output-format", fmt, output=out)
            parent = rgba(all_tiles(out)[(0, 0, 0)])
            self.assertEqual(parent.getpixel((2, 10)), (128, 0, 128, 255))
            self.assertEqual(parent.getpixel((10, 2)), (0, 0, 0, 0))
        # Invisible green must not bleed into the visible red at a 2x2 average.
        edge = image((0, 255, 0, 0))
        for y in range(SIZE):
            for x in range(0, SIZE, 2):
                edge.putpixel((x, y), (255, 0, 0, 255))
        c = self.root / "edge.mbtiles"
        fixture(c, [(1, 0, 0, png(edge))])
        self.cli("--input", c, "--output-format", "png")
        self.assertEqual(rgba(all_tiles(self.out)[(0, 0, 0)]).getpixel((2, 10)), (255, 0, 0, 128))

    def test_lossy_webp_is_transcoded_even_without_overlap(self):
        im = image((200, 90, 30, 150))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=10, lossless=False)
        lossy = buf.getvalue()
        with m.decode(lossy, SIZE, m.MIB):
            self.assertFalse(m.lossless_webp(lossy))
        a = self.root / "lossy.mbtiles"
        fixture(a, [(1, 0, 0, lossy)], {"format": "webp"})
        for mode in ("pyramid", "overlay"):
            out = self.root / f"{mode}.mbtiles"
            self.cli("--mode", mode, "--input", a, output=out)
            got = all_tiles(out)
            self.assertNotEqual(got[(1, 0, 0)], lossy)
            self.pixels_equal(got[(1, 0, 0)], rgba(lossy))  # preserve decoded input, not lost original detail
            for data in got.values():
                with m.decode(data, SIZE, m.MIB):
                    self.assertTrue(m.lossless_webp(data))
            # verify --decode detects an externally inserted lossy tile too.
            with sqlite3.connect(out) as db:
                db.execute("UPDATE tiles SET tile_data=? WHERE zoom_level=1", (lossy,))
            result = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "verify",
                                     "--output", str(out), "--decode"], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Lossy WebP", result.stderr)

    def test_common_base_validation_and_frozen_configuration(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        blob = webp(image((20, 30, 40, 255)))
        fixture(a, [(3, 1, 1, blob), (2, 0, 0, blob)])
        fixture(b, [(2, 1, 0, blob)])
        result = self.cli("--input", a, "--input", b, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different highest zooms", result.stderr)
        self.assertEqual(all_tiles(self.out), {})
        chosen = self.out  # fix a rejected pristine plan without deleting its empty output
        self.cli("--input", a, "--input", b, "--base-zoom", "2", "--min-zoom", "1", output=chosen)
        self.assertEqual({z for z, x, y in all_tiles(chosen)}, {1, 2})
        before = all_tiles(chosen)
        for flags in (("--base-zoom", "3"), ("--min-zoom", "0"), ("--mode", "overlay")):
            result = self.cli(*flags, output=chosen, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Resume requires", result.stderr)
            self.assertEqual(all_tiles(chosen), before)
        missing = self.root / "missing.mbtiles"
        result = self.cli("--input", b, "--base-zoom", "3", output=missing, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no tiles at base zoom", result.stderr)
        self.assertEqual(all_tiles(missing), {})
        invalid = self.cli("--input", a, "--base-zoom", "3", "--min-zoom", "4",
                           output=self.root / "range.mbtiles", check=False)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("--min-zoom must be <=", invalid.stderr)

    def test_incremental_rebuild_only_touched_ancestors_matches_fresh_output(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(a, [(4, 0, 0, png(image((200, 0, 0, 180)))),
                    (4, 15, 15, png(image((0, 200, 0, 255))))])
        fixture(b, [(4, 0, 0, webp(image((0, 0, 200, 100))))])
        for fmt in ("png", "webp"):
            out, fresh = self.root / f"inc_{fmt}.mbtiles", self.root / f"fresh_{fmt}.mbtiles"
            first = self.cli("--input", a, "--output-format", fmt, output=out)
            self.assertEqual(json.loads(first.stderr.splitlines()[-1])["overview_tiles"], 7)
            before = all_tiles(out)
            added = self.cli("--input", b, output=out)
            self.assertEqual(json.loads(added.stderr.splitlines()[-1])["overview_tiles"], 4)
            after = all_tiles(out)
            for key in ((4, 15, 15), (3, 7, 7), (2, 3, 3), (1, 1, 1)):
                self.assertEqual(before[key], after[key])
            self.cli("--input", a, "--input", b, "--output-format", fmt, output=fresh)
            self.assertEqual(after, all_tiles(fresh))
            replay = self.cli("--input", a, "--input", b, output=out)
            self.assertEqual(json.loads(replay.stderr.splitlines()[-1])["overview_tiles"], 0)
            with sqlite3.connect(out) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM _merge_dirty").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT phase,overviews_written FROM _merge_state").fetchone(), ("complete", 11))

    def test_many_sources_share_low_zoom_but_overviews_are_generated_once(self):
        sources = self.root / "sources"
        sources.mkdir()
        blob = png(image((40, 80, 120, 255)))
        # All inputs deliberately contain the same bad z=1 overview. Only their
        # z=8 keys are imported; dirty parents deduplicate on disk across sources.
        for i in range(96):
            fixture(sources / f"{i:03}.mbtiles", [(8, 128 + i % 12, 128 + i // 12, blob),
                                                (1, 1, 1, b"ignored")])
        result = self.cli("--input-dir", sources, "--max-tile-mib", "1", "--batch-mib", "5",
                          "--batch-size", "100", "--output-format", "png")
        stats = json.loads(result.stderr.splitlines()[-1])
        got = all_tiles(self.out)
        parents = set()
        for z, x, y in [k for k in got if k[0] == 8]:
            while z > 0:
                z, x, y = z - 1, x // 2, y // 2
                parents.add((z, x, y))
        self.assertEqual(stats["overview_tiles"], len(parents))
        self.assertEqual(len(got), 96 + len(parents))
        self.assertLessEqual(stats["peak_batch_bytes"], 5 * m.MIB)
        self.assertLessEqual(stats["peak_inflight"], 2)
        self.assertEqual(len([k for k in got if k[0] == 1]), 1)

    def test_late_base_batch_seeks_index_without_rescanning_earlier_tiles(self):
        # A zoom equality combined with a comparison of (z,x,y) can silently
        # degrade to a zoom-only seek. Measure the actual source VM work rather
        # than testing an independently written query or a fragile plan string.
        args = m.parser().parse_args(["merge", "--output", str(self.out), "--base-zoom", "14",
                                      "--output-format", "png", "--batch-size", "2"])
        dst, settings = m.open_output(self.out, args)
        src = sqlite3.connect(":memory:")
        try:
            src.execute("CREATE TABLE tiles(zoom_level,tile_column,tile_row,tile_data,PRIMARY KEY(zoom_level,tile_column,tile_row))")
            data = png(image((10, 20, 30, 255)))
            src.executemany("INSERT INTO tiles VALUES(14,?,0,?)", ((x, data) for x in range(10000)))
            steps = 0
            def progress():
                nonlocal steps
                steps += 1
                return 0
            src.set_progress_handler(progress, 1)
            jobs, budget = m.read_batch(src, dst, (14, 9990, 0), args, "png")
            self.assertEqual([job[:3] for job in jobs], [(14, 9991, 0), (14, 9992, 0)])
            self.assertLess(steps, 2000, "Source keyset pagination rescanned earlier tiles")
        finally:
            src.close()
            dst.close()

    @unittest.skipUnless(os.name == "posix", "SIGKILL requires POSIX")
    def test_sigkill_import_and_overview_transactions_resume_exactly(self):
        a, b, c = [self.root / f"{s}.mbtiles" for s in "abc"]
        for path, color in ((a, (240, 0, 0, 150)), (b, (0, 0, 240, 100)), (c, (0, 240, 0, 70))):
            fixture(path, [(4, x, 2, png(image(color))) for x in range(8)])
        driver = self.root / "crash_pyramid.py"
        driver.write_text('''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import merge_mbtiles as m
original = m._commit
def crash(db):
    phase, written = db.execute("SELECT phase,overviews_written FROM _merge_state").fetchone()
    row = db.execute("SELECT tile_count FROM _merge_sources WHERE path=?", (sys.argv[3],)).fetchone()
    target = (phase == "importing" and row is not None and row[0] == 4) if sys.argv[7] == "import" else (phase == "building" and written == int(sys.argv[8]))
    if target and sys.argv[5] == "before":
        Path(sys.argv[4]).touch()
        while True: time.sleep(1)
    original(db)
    if target and sys.argv[5] == "after":
        Path(sys.argv[4]).touch()
        while True: time.sleep(1)
if __name__ == "__main__":
    m._commit = crash
    raise SystemExit(m.main(["merge", "--output", sys.argv[2], "--input", sys.argv[3], "--tile-size", "16", "--workers", "2", "--batch-size", "2", "--output-format", sys.argv[6]]))
''', encoding="utf-8")
        for fmt in ("png", "webp"):
            golden = self.root / f"golden_{fmt}.mbtiles"
            self.cli("--input", a, "--input", b, "--input", c, "--output-format", fmt, output=golden)
            expected = all_tiles(golden)
            for stage in ("import", "overview", "overview-final"):
                for boundary in ("before", "after"):
                    with self.subTest(format=fmt, stage=stage, boundary=boundary):
                        out = self.root / f"{fmt}_{stage}_{boundary}.mbtiles"
                        self.cli("--input", a, "--output-format", fmt, output=out)
                        with sqlite3.connect(out) as db:
                            baseline = db.execute("SELECT overviews_written FROM _merge_state").fetchone()[0]
                            target = baseline + (8 if stage == "overview-final" else 2)
                        marker = self.root / f"{out.stem}.ready"
                        logfile = self.root / f"{out.stem}.log"
                        with logfile.open("w") as log:
                            proc = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(out), str(b.resolve()),
                                                     str(marker), boundary, fmt, stage, str(target)],
                                                    stdout=log, stderr=log, start_new_session=True)
                            try:
                                deadline = time.monotonic() + 30
                                while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                                    time.sleep(.03)
                                self.assertTrue(marker.exists(), logfile.read_text())
                            finally:
                                if proc.poll() is None:
                                    os.killpg(proc.pid, signal.SIGKILL)
                                proc.wait(timeout=10)
                        with sqlite3.connect(out) as db:
                            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                            pending = db.execute("SELECT count(*) FROM _merge_dirty").fetchone()[0]
                            if stage == "overview-final" and boundary == "after":
                                self.assertEqual(pending, 0)  # final pixels committed, phase still building
                            else:
                                self.assertGreater(pending, 0)
                            written = db.execute("SELECT overviews_written FROM _merge_state").fetchone()[0]
                            expected_written = (baseline if stage == "import" else target if boundary == "after"
                                                else baseline if stage == "overview" else target - 1)
                            self.assertEqual(written, expected_written)
                            imported = db.execute("SELECT tile_count FROM _merge_sources WHERE path=?", (str(b.resolve()),)).fetchone()[0]
                            self.assertEqual(imported, (2 if boundary == "before" else 4) if stage == "import" else 8)
                        check = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "verify", "--output", str(out)], capture_output=True, text=True)
                        self.assertNotEqual(check.returncode, 0)
                        self.assertIn("incomplete", check.stderr)
                        # The interrupted b is deliberately omitted. Import it
                        # before c if active, then rebuild old and new dirty keys.
                        self.cli("--input", c, output=out)
                        self.assertEqual(all_tiles(out), expected)
                        self.cli("--input", b, output=out)
                        self.assertEqual(all_tiles(out), expected)


if __name__ == "__main__":
    unittest.main()
