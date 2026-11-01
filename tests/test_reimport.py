# SPDX-License-Identifier: MIT
"""Reimport of changed/listed sources, stat verification and empty tiles.

Oracles are fresh outputs built from the same final inputs: a reimport must
leave exactly the pixels a clean build would, wherever the new version covers
the old one. The partial-alpha test documents the one case where it cannot.
"""
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
from test_merge import ROOT, all_tiles, fixture, image, rgba
from test_png import png


def events(proc):
    return [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]


def finished(proc):
    return next(e for e in reversed(events(proc)) if e["event"] == "finished")


class ReimportTests(unittest.TestCase):
    setUp = support.MergeTests.setUp
    tearDown = support.MergeTests.tearDown

    def cli(self, *extra, **kwargs):
        return support.MergeTests.cli(self, "--mode", "pyramid", "--output-format", "png", *extra, **kwargs)

    def plan(self, tiles):
        """Neighbouring opaque obce-like tiles at z4 with binary-alpha edges."""
        return [(4, x, y, png(im)) for (x, y), im in tiles.items()]

    def test_changed_source_refused_without_flag_then_overlaid_like_a_fresh_build(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(b, self.plan({(1, 1): image((0, 160, 0, 255)), (2, 1): image((0, 160, 0, 255))}))
        fixture(a, self.plan({(1, 1): image((200, 0, 0, 255), (0, 0, 9, 15))}))
        self.cli("--input", b, "--input", a)
        a.unlink()  # new version of the same plan at the same path, covering more
        fixture(a, self.plan({(1, 1): image((0, 0, 200, 255), (0, 0, 11, 15)),
                              (3, 1): image((0, 0, 200, 255))}))
        refused = self.cli("--input", a, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--reimport changed", refused.stderr)
        done = self.cli("--input", a, "--reimport", "changed")
        self.assertEqual(finished(done)["reimported"], 1)
        fresh = self.root / "fresh.mbtiles"
        self.cli("--input", b, "--input", a, output=fresh)
        self.assertEqual(all_tiles(self.out), all_tiles(fresh))  # base and every regenerated parent
        unchanged = self.cli("--input", a, "--reimport", "changed")
        self.assertEqual(finished(unchanged)["reimported"], 0)

    def test_reimport_always_is_idempotent_for_binary_alpha(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(b, self.plan({(1, 1): image((0, 160, 0, 255))}))
        fixture(a, self.plan({(1, 1): image((200, 0, 0, 255), (0, 0, 7, 15)),
                              (2, 1): image((200, 0, 0, 255), (0, 0, 15, 4))}))
        self.cli("--input", b, "--input", a)
        first = all_tiles(self.out)
        for _ in range(3):
            again = self.cli("--input", a, "--reimport", "always")
            self.assertEqual(finished(again)["reimported"], 1)
            self.assertEqual(all_tiles(self.out), first)

    def test_reimport_of_partial_alpha_accumulates_documented_limit(self):
        a = self.root / "a.mbtiles"
        fixture(a, self.plan({(1, 1): image((200, 0, 0, 128))}))
        self.cli("--input", a)
        self.cli("--input", a, "--reimport", "always")
        alpha = rgba(all_tiles(self.out)[(4, 1, 1)]).getpixel((0, 0))[3]
        self.assertEqual(alpha, 192)  # 128 over 128: why sources should have 0/255 alpha

    def test_reimport_never_moves_unlisted_sources_and_keeps_priority_of_listed_order(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(a, self.plan({(1, 1): image((200, 0, 0, 255))}))
        fixture(b, self.plan({(1, 1): image((0, 0, 200, 255))}))
        self.cli("--input", a, "--input", b)  # b on top
        self.cli("--input", a, "--reimport", "always")  # a re-applied, now on top
        self.assertEqual(rgba(all_tiles(self.out)[(4, 1, 1)]).getpixel((0, 0)), (200, 0, 0, 255))

    def test_verify_recorded_stat_hashes_only_changed_stat_and_catches_restored_mtime(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(a, self.plan({(1, 1): image((200, 0, 0, 255))}))
        fixture(b, self.plan({(2, 1): image((0, 0, 200, 255))}))
        self.cli("--input", a, "--input", b)
        os.utime(a)  # touched, identical bytes: still unchanged
        self.cli("--verify-recorded", "stat", "--input", b)
        with sqlite3.connect(self.out) as db:
            fp = json.loads(db.execute("SELECT fingerprint FROM _merge_sources WHERE path=?", (str(a.resolve()),)).fetchone()[0])
        self.assertEqual(fp[3], a.stat().st_mtime_ns)  # stat remembered after the hash matched
        stat = a.stat()
        with sqlite3.connect(a) as db:  # same size, different pixels, mtime restored
            db.execute("UPDATE tiles SET tile_data=?", (png(image((201, 0, 0, 255))),))
        os.utime(a, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        changed = self.cli("--verify-recorded", "stat", "--input", b, check=False)
        self.assertEqual(changed.returncode, 1)
        self.assertIn("Changed imported input", changed.stderr)

    def test_fully_transparent_tiles_are_skipped_unless_kept(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        empty = image((0, 0, 0, 0))
        fixture(a, self.plan({(1, 1): image((0, 160, 0, 255)), (2, 1): empty}))
        fixture(b, self.plan({(1, 1): empty, (3, 1): empty}))
        proc = self.cli("--input", a, "--input", b)
        got = all_tiles(self.out)
        self.assertNotIn((4, 2, 1), got)
        self.assertNotIn((4, 3, 1), got)
        self.assertEqual(finished(proc)["empty_skipped"], 3)
        self.assertEqual(rgba(got[(4, 1, 1)]).getpixel((0, 0)), (0, 160, 0, 255))
        kept = self.root / "kept.mbtiles"
        self.cli("--input", a, "--keep-empty", output=kept)
        self.assertIn((4, 2, 1), all_tiles(kept))

    @unittest.skipUnless(os.name == "posix", "SIGKILL requires POSIX")
    def test_sigkill_during_reimport_resumes_without_double_overlay(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        fixture(b, self.plan({(x, 2): image((0, 160, 0, 255)) for x in range(8)}))
        # Partial alpha makes every extra overlay visible, so a replayed batch would fail.
        fixture(a, self.plan({(x, 2): image((200, 0, 0, 120), (0, 0, 7, 15)) for x in range(8)}))
        golden = self.root / "golden.mbtiles"
        self.cli("--input", b, "--input", a, output=golden)
        self.cli("--input", a, "--reimport", "always", output=golden)  # exactly one reimport
        self.cli("--input", b, "--input", a)
        driver = self.root / "crash.py"
        driver.write_text('''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import merge_mbtiles as m
original = m._commit
def crash(db):
    row = db.execute("SELECT status,last_x FROM _merge_sources WHERE path=?", (sys.argv[3],)).fetchone()
    original(db)
    if row and row[0] == "active" and row[1] is not None and row[1] >= 3:
        Path(sys.argv[4]).touch()
        while True: time.sleep(1)
if __name__ == "__main__":  # spawned workers re-import this module
    m._commit = crash
    raise SystemExit(m.main(["merge", "--output", sys.argv[2], "--input", sys.argv[3], "--tile-size", "16",
                             "--workers", "2", "--batch-size", "2", "--reimport", "always"]))
''', encoding="utf-8")
        marker = self.root / "ready"
        proc = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(self.out), str(a.resolve()), str(marker)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(marker.exists())
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        with sqlite3.connect(self.out) as db:
            self.assertEqual(db.execute("SELECT status FROM _merge_sources WHERE path=?", (str(a.resolve()),)).fetchone()[0], "active")
        self.cli()  # no inputs: the active reimport resumes at its committed cursor
        self.assertEqual(all_tiles(self.out), all_tiles(golden))


if __name__ == "__main__":
    unittest.main()
