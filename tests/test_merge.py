# SPDX-License-Identifier: MIT
"""Integration tests for source order, transactions, resource limits and resume.

Small 16px fixtures keep correctness tests fast. CLI subprocesses exercise real
spawned workers; SIGKILL targets the process group so workers do not remain.
512px demonstration checks and the larger RSS benchmark complement this suite.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import merge_mbtiles as m

SIZE = 16


def image(color, box=None, size=SIZE):
    """Create an RGBA oracle with a transparent background outside the box."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if box is None:
        im.paste(color, (0, 0, size, size))
    else:
        ImageDraw.Draw(im).rectangle(box, fill=color)
    return im


def webp(im):
    """Keep transparent RGB samples exact so byte/pixel checks are meaningful."""
    out = io.BytesIO()
    im.save(out, "WEBP", lossless=True, exact=True)
    return out.getvalue()


def fixture(path, rows, metadata=None, indexed=True):
    """Build a standalone flat input; omitting its index creates a bad fixture."""
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)")
        if indexed:
            db.execute("CREATE UNIQUE INDEX tile_index ON tiles(zoom_level,tile_column,tile_row)")
        db.executemany("INSERT INTO tiles VALUES(?,?,?,?)", rows)
        db.execute("CREATE TABLE metadata(name TEXT,value TEXT)")
        db.executemany("INSERT INTO metadata VALUES(?,?)", (metadata or {}).items())


def all_tiles(path):
    """Materialize only small test archives for deterministic blob comparisons."""
    with sqlite3.connect(path) as db:
        return dict((tuple(r[:3]), r[3]) for r in db.execute("SELECT * FROM tiles ORDER BY 1,2,3"))


def rgba(data):
    """Decode oracle bytes independently of the merger validation functions."""
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGBA")


class MergeTests(unittest.TestCase):
    """Exercise the public CLI and deliberately unsafe database/source states."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out.mbtiles"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *extra, output=None, check=True):
        """Run a real CLI with tiny batches, preserving stderr for assertions."""
        cmd = [sys.executable, str(ROOT / "merge_mbtiles.py"), "merge",
               "--output", str(output or self.out), "--tile-size", str(SIZE),
               "--workers", "2", "--max-inflight", "2", "--batch-size", "2", "--json-log"]
        proc = subprocess.run(cmd + list(map(str, extra)), capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def pixels_equal(self, got, expected):
        self.assertEqual(rgba(got).tobytes(), expected.tobytes())

    def test_alpha_priority_tms_metadata_and_incremental(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red = image((240, 0, 0, 200), (0, 0, 9, 15))
        blue = image((0, 0, 240, 128), (6, 0, 15, 15))
        green = image((0, 255, 0, 255))
        red_blob, blue_blob, green_blob = map(webp, (red, blue, green))
        fixture(a, [(2, 1, 0, red_blob), (3, 2, 1, green_blob)],
                {"format": "webp", "bounds": "12,48,14,50", "minzoom": "99", "attribution": "A"})
        fixture(b, [(2, 1, 0, blue_blob)], {"bounds": "13,49,16,51", "attribution": "B"})
        self.cli("--input", a, "--name", "Raster overlay – α", "--description", "Test")
        proc = self.cli("--input", b)
        events = [json.loads(line) for line in proc.stderr.splitlines()]
        self.assertLessEqual(max(e.get("peak_inflight", 0) for e in events), 2)
        got = all_tiles(self.out)
        self.assertEqual(set(got), {(2, 1, 0), (3, 2, 1)})  # TMS row untouched
        self.pixels_equal(got[(2, 1, 0)], Image.alpha_composite(red, blue))
        self.assertEqual(got[(3, 2, 1)], green_blob)  # no recompression
        with sqlite3.connect(self.out) as db:
            meta = dict(db.execute("SELECT * FROM metadata"))
            self.assertEqual(meta["bounds"], "12,48,16,51")
            self.assertEqual(meta["center"], "14,49.5,2")
            self.assertEqual((meta["minzoom"], meta["maxzoom"]), ("2", "3"))
            self.assertEqual(meta["name"], "Raster overlay – α")
            self.assertEqual(meta["attribution"], "A | B")
        before = all_tiles(self.out)
        proc = self.cli("--input", a, "--input", b)
        self.assertIn('"skipped": 2', proc.stderr)
        self.assertEqual(all_tiles(self.out), before)
        proc = subprocess.run([sys.executable, str(ROOT / "merge_mbtiles.py"), "verify", "--output", str(self.out), "--decode"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["decoded_tiles"], 2)

    def test_bounds_fallback_southern_tms_and_wrap(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(2, 1, 0, webp(image((1, 2, 3, 255))))], {"bounds": "nan,0,10,5"})
        self.cli("--input", a)
        with sqlite3.connect(self.out) as db:
            bounds = list(map(float, dict(db.execute("SELECT * FROM metadata"))["bounds"].split(",")))
        for a, b in zip(bounds, m.tile_bounds(2, 1, 0)):
            self.assertAlmostEqual(a, b, places=8)
        self.assertLess(bounds[3], 0)
        self.assertEqual(m.parse_bounds("170,-20,-170,20"), [-180.0, -20, 180.0, 20])

    def test_changed_source_same_size_and_mtime_refused_before_new_writes(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(a, [(1, 0, 0, webp(image((10, 20, 30, 128))))], {"attribution": "AAA"})
        fixture(b, [(1, 1, 0, webp(image((0, 1, 2, 255))))])
        self.cli("--input", a)
        saved = a.read_bytes()
        stamp = a.stat()
        raw = saved.replace(b"AAA", b"BBB")
        self.assertNotEqual(raw, saved)
        a.write_bytes(raw)
        os.utime(a, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(a.stat().st_size, stamp.st_size)
        old = all_tiles(self.out)
        proc = self.cli("--input", b, check=False)  # a omitted, still verified
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Changed imported input", proc.stderr)
        self.assertEqual(all_tiles(self.out), old)
        a.write_bytes(saved)
        self.cli("--input", b)  # exact restored bytes are safe
        self.assertEqual(len(all_tiles(self.out)), 2)

    def test_reject_corrupt_unique_tiles_sizes_coords_and_schema(self):
        cases = [([(1, 0, 0, b"garbage")], True, {}),
                 ([(1, 0, 0, webp(image((1, 2, 3, 255), size=32)))], True, {}),
                 ([(1, 0, 2, webp(image((1, 2, 3, 255))))], True, {}),
                 ([(1, 0, 0, webp(image((1, 2, 3, 255))))], False, {}),
                 ([(1, 0, 0, webp(image((1, 2, 3, 255))))], True, {"scheme": "xyz"})]
        for i, (rows, index, meta) in enumerate(cases):
            with self.subTest(i=i):
                a, out = self.root / f"bad{i}.mbtiles", self.root / f"out{i}.mbtiles"
                fixture(a, rows, meta, index)
                proc = self.cli("--input", a, output=out, check=False)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(all_tiles(out), {})

    def test_byte_budget_not_only_row_count(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        rows = [(4, x, 0, webp(image((x, 2, 3, 100)))) for x in range(10)]
        fixture(a, rows)
        fixture(b, rows)
        self.cli("--input", a)
        proc = self.cli("--input", b, "--batch-size", "100", "--max-tile-mib", "1", "--batch-mib", "3")
        final = json.loads(proc.stderr.splitlines()[-1])
        self.assertGreaterEqual(final["batches"], 4)  # output reserve bounds tiny compressed inputs too
        self.assertLessEqual(final["peak_batch_bytes"], 3 * m.MIB)
        self.assertLessEqual(final["peak_inflight"], 2)

    def test_worker_failure_rolls_back_already_written_sql_batch(self):
        a = self.root / "broken_late.mbtiles"
        fixture(a, [(3, x, 0, webp(image((1, 2, 3, 100))) if x < 3 else b"broken") for x in range(4)])
        proc = self.cli("--input", a, "--workers", "1", "--max-inflight", "1",
                        "--batch-size", "4", "--sql-batch-size", "1", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(all_tiles(self.out), {})
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute("SELECT tile_count FROM _merge_sources").fetchone()[0], 0)

    def test_blocked_wal_checkpoint_stops_then_resumes(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red, blue = image((200, 0, 0, 128)), image((0, 0, 200, 128))
        fixture(a, [(3, x, 0, webp(red)) for x in range(4)])
        fixture(b, [(3, x, 0, webp(blue)) for x in range(4)])
        self.cli("--input", a)
        reader = sqlite3.connect(self.out)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            proc = self.cli("--input", b, "--busy-timeout", "1", check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checkpoint blocked", proc.stderr)
            with sqlite3.connect(self.out) as db:
                row = db.execute("SELECT tile_count,status FROM _merge_sources WHERE path=?", (str(b.resolve()),)).fetchone()
                # The new source registration is itself checkpointed, so a
                # pre-existing reader stops us before any tile payload is added.
                self.assertEqual(row, (0, "active"))
        finally:
            reader.close()
        self.cli("--input", b)
        for data in all_tiles(self.out).values():
            self.pixels_equal(data, Image.alpha_composite(red, blue))

    def test_reject_oversized_blob_before_fetch_and_decoder_bomb(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(1, 0, 0, b"x" * (m.MIB + 1))])
        proc = self.cli("--input", a, "--max-tile-mib", "1", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("oversized", proc.stderr)
        data = bytearray(webp(image((1, 2, 3, 128))))
        offset = data.index(b"VP8L") + 8 + 1
        bits = int.from_bytes(data[offset:offset + 4], "little")
        bits = (bits & ~0x0fffffff) | 0x0fffffff  # max WebP dimensions
        data[offset:offset + 4] = bits.to_bytes(4, "little")
        with self.assertRaisesRegex(m.MergeError, "Expected"):
            m.decode(bytes(data), SIZE, 4 * m.MIB)

    def test_output_input_unmanaged_and_exclusive_lock(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(1, 0, 0, webp(image((1, 2, 3, 255))))])
        saved = a.read_bytes()
        self.assertNotEqual(self.cli("--input", a, output=a, check=False).returncode, 0)
        self.assertEqual(a.read_bytes(), saved)
        proc = self.cli("--input", a, output=a, check=False)
        self.assertNotEqual(proc.returncode, 0)
        proc = self.cli("--input", a, output=self.root / "unmanaged.mbtiles", check=False)  # make managed output
        self.assertEqual(proc.returncode, 0)
        proc = self.cli(output=a, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(a.read_bytes(), saved)  # refusing adoption must not change journal mode
        with m.output_lock(self.out):
            proc = self.cli("--input", a, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("holds the output lock", proc.stderr)

    def test_wal_input_refused(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(1, 0, 0, webp(image((1, 2, 3, 255))))])
        with sqlite3.connect(a) as db:
            db.execute("PRAGMA journal_mode=WAL")
        proc = self.cli("--input", a, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("WAL input", proc.stderr)

    def test_source_mutation_marks_tainted_and_blocks_resume(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(2, x, 0, webp(image((x, 2, 3, 100)))) for x in range(4)])
        args = m.parser().parse_args(["merge", "--output", str(self.out), "--input", str(a), "--tile-size", str(SIZE), "--workers", "1", "--batch-size", "1"])
        args.max_inflight = 1
        original = m.assert_static
        triggered = False
        def changed(path, stamp):
            nonlocal triggered
            if self.out.exists():
                with sqlite3.connect(self.out) as db:
                    r = db.execute("SELECT sum(tile_count) FROM _merge_sources").fetchone()
                if r[0] and not triggered:
                    triggered = True
                    raise m.SourceChanged("simulated out-of-band file replacement")
            return original(path, stamp)
        with mock.patch.object(m, "assert_static", side_effect=changed):
            with self.assertRaises(m.SourceChanged):
                m.merge(args)
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute("SELECT status FROM _merge_sources").fetchone()[0], "tainted")
        proc = self.cli("--input", a, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tainted", proc.stderr)

    def test_keyset_query_plan_uses_indexes(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(4, x, 0, webp(image((x, 0, 0, 255)))) for x in range(10)])
        with sqlite3.connect(a) as db:
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles WHERE (zoom_level,tile_column,tile_row)>(?,?,?) ORDER BY zoom_level,tile_column,tile_row LIMIT 2", (4, 0, 0)))
            self.assertIn("SEARCH tiles USING INDEX tile_index", plan)
            self.assertNotIn("TEMP B-TREE", plan)
        self.cli("--input", a)
        with sqlite3.connect(self.out) as db:
            db.execute("CREATE TEMP TABLE batch_keys(z,x,y)")
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN SELECT t.tile_data FROM batch_keys b CROSS JOIN tiles t WHERE t.zoom_level=b.z AND t.tile_column=b.x AND t.tile_row=b.y"))
            self.assertIn("SEARCH t USING INDEX", plan)

    def test_manifest_order_empty_sources_and_default_directory_sort(self):
        directory = self.root / "sources"
        directory.mkdir()
        a, b, empty = directory / "a.mbtiles", directory / "b.mbtiles", directory / "empty.mbtiles"
        one, two = image((255, 0, 0, 100)), image((0, 0, 255, 100))
        fixture(a, [(1, 0, 0, webp(one))])
        fixture(b, [(1, 0, 0, webp(two))])
        fixture(empty, [])
        self.cli("--input-dir", directory)
        self.pixels_equal(all_tiles(self.out)[(1, 0, 0)], Image.alpha_composite(one, two))
        manifest = directory / "order.txt"
        manifest.write_text("# explicit priority\nb.mbtiles\na.mbtiles\n", encoding="utf-8")
        other = self.root / "other.mbtiles"
        self.cli("--input-list", manifest, output=other)
        self.pixels_equal(all_tiles(other)[(1, 0, 0)], Image.alpha_composite(two, one))

    @unittest.skipUnless(os.name == "posix", "SIGKILL requires POSIX")
    def test_sigkill_before_and_after_commit_exact_resume_and_active_order(self):
        a, b, c = [self.root / f"{s}.mbtiles" for s in "abc"]
        red, blue, green = image((240, 0, 0, 150)), image((0, 0, 240, 100)), image((0, 240, 0, 70))
        keys = [(4, x, 2) for x in range(8)]
        fixture(a, [(*key, webp(red)) for key in keys])
        blue_png = io.BytesIO()
        blue.save(blue_png, "PNG")
        fixture(b, [(*key, blue_png.getvalue()) for key in keys], {"format": "png"})
        fixture(c, [(*key, webp(green)) for key in keys])
        for output_format in ("webp", "png"):
            golden = self.root / f"golden_{output_format}.mbtiles"
            self.cli("--input", a, "--input", b, "--input", c, "--output-format", output_format, output=golden)
            expected = all_tiles(golden)
            for mode in ("before", "after"):
                with self.subTest(mode=mode, output_format=output_format):
                    out = self.root / f"{mode}_{output_format}.mbtiles"
                    self.cli("--input", a, "--output-format", output_format, output=out)
                    marker = self.root / f"{mode}_{output_format}.ready"
                    driver = self.root / f"crash_{mode}.py"
                    # The child pauses exactly around commit, signals readiness
                    # with a file, then the parent kills its entire process group.
                    # __main__ guards in this driver are essential for spawn.
                    driver.write_text('''
import sys, os, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import merge_mbtiles as m
original = m._commit
def crash(db):
    row = db.execute("SELECT tile_count FROM _merge_sources WHERE path=?", (sys.argv[3],)).fetchone()
    target = row is not None and row[0] == 4
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
                    with (self.root / f"{mode}.log").open("w") as logfile:
                        proc = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(out), str(b.resolve()), str(marker), mode, output_format],
                                                stdout=logfile, stderr=logfile, start_new_session=True)
                        deadline = time.monotonic() + 30
                        try:
                            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                                time.sleep(.03)
                            self.assertTrue(marker.exists(), (self.root / f"{mode}.log").read_text())
                        finally:
                            if proc.poll() is None:
                                os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=10)
                    with sqlite3.connect(out) as db:
                        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        count = db.execute("SELECT tile_count FROM _merge_sources WHERE path=?", (str(b.resolve()),)).fetchone()[0]
                        self.assertEqual(count, 2 if mode == "before" else 4)
                    # b omitted: active contribution still has to finish before new c.
                    self.cli("--input", c, output=out)
                    self.assertEqual(all_tiles(out), expected)
                    self.cli("--input", b, output=out)
                    self.assertEqual(all_tiles(out), expected)


if __name__ == "__main__":
    unittest.main()
