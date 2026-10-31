# SPDX-License-Identifier: MIT
"""PNG/WebP conversion, exact pixels, validation limits and ledger migration.

Reuse test helpers through a module, not an imported TestCase class: unittest
would otherwise discover that class again and run the base suite twice.
"""
import io
import json
import sqlite3
import struct
import subprocess
import sys
import unittest
from unittest import mock
import zlib

from PIL import Image
import test_merge as support
from test_merge import ROOT, SIZE, all_tiles, fixture, image, m, rgba, webp


def png(im, level=6):
    """Encode an independent PNG fixture at a known compression effort."""
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=level)
    return buf.getvalue()


class PNGTests(unittest.TestCase):
    """Keep output encoding uniform across mixed inputs and resumed imports."""
    setUp = support.MergeTests.setUp
    tearDown = support.MergeTests.tearDown
    cli = support.MergeTests.cli
    pixels_equal = support.MergeTests.pixels_equal

    def test_png_input_output_overlap_and_original_bytes(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red, blue = image((240, 0, 0, 160), (0, 0, 9, 15)), image((0, 0, 240, 128), (6, 0, 15, 15))
        original = png(red, 0)
        fixture(a, [(2, 0, 0, original), (2, 1, 0, original)], {"format": "png"})
        fixture(b, [(2, 1, 0, png(blue))], {"format": "PNG"})
        self.cli("--input", a, "--input", b, "--output-format", "png")
        got = all_tiles(self.out)
        self.assertEqual(got[(2, 0, 0)], original)
        self.pixels_equal(got[(2, 1, 0)], Image.alpha_composite(red, blue))
        with sqlite3.connect(self.out) as db:
            self.assertEqual(dict(db.execute("SELECT * FROM metadata"))["format"], "png")
        check = subprocess.run([sys.executable, str(ROOT / 'merge_mbtiles.py'), 'verify', '--output', str(self.out), '--decode'], capture_output=True, text=True, timeout=30)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(json.loads(check.stdout)["decoded_tiles"], 2)

    def test_mixed_sources_uniform_output_both_formats(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red, blue = image((240, 0, 0, 160)), image((0, 0, 240, 128))
        aw, bp = webp(red), png(blue)
        fixture(a, [(2, 0, 0, aw), (2, 1, 0, aw)], {"format": "webp"})
        fixture(b, [(2, 1, 0, bp), (2, 2, 0, bp)], {"format": "png"})
        for fmt in ("png", "webp"):
            with self.subTest(fmt=fmt):
                out = self.root / f"{fmt}.mbtiles"
                self.cli("--input", a, "--input", b, "--output-format", fmt, output=out)
                got = all_tiles(out)
                self.assertEqual({m.image_format(data) for data in got.values()}, {fmt})
                self.pixels_equal(got[(2, 0, 0)], red)
                self.pixels_equal(got[(2, 1, 0)], Image.alpha_composite(red, blue))
                self.pixels_equal(got[(2, 2, 0)], blue)
                self.assertEqual(got[(2, 0, 0)] if fmt == "webp" else got[(2, 2, 0)], aw if fmt == "webp" else bp)
                replay = self.cli("--input", a, "--input", b, output=out)
                self.assertIn('"skipped": 2', replay.stderr)
                self.assertEqual(all_tiles(out), got)

    def test_autodetect_missing_metadata_and_paletted_gray_rgb_png(self):
        palette = Image.new("P", (SIZE, SIZE), 1)
        palette.putpalette([0, 0, 0, 250, 100, 50] + [0] * (768 - 6))
        palette.info["transparency"] = bytes((0, 100))
        variants = [palette, Image.new("L", (SIZE, SIZE), 40),
                    Image.new("LA", (SIZE, SIZE), (40, 80)), Image.new("RGB", (SIZE, SIZE), (1, 2, 3))]
        rows = [(3, i, 0, png(im)) for i, im in enumerate(variants)]
        rows.append((3, 4, 0, webp(image((1, 2, 3, 80)))))
        a = self.root / "mixed_unlabeled.mbtiles"
        fixture(a, rows)
        self.cli("--input", a, "--output-format", "png")
        got = all_tiles(self.out)
        self.assertEqual({m.image_format(v) for v in got.values()}, {"png"})
        for z, x, y, original in rows:
            self.assertEqual(rgba(got[(z, x, y)]).tobytes(), rgba(original).tobytes())
            if x < 4:
                self.assertEqual(got[(z, x, y)], original)

    def test_png_bombs_crc_animation_16bit_and_metadata_mismatch(self):
        data = bytearray(png(image((1, 2, 3, 128))))
        data[16:20] = struct.pack(">I", 1000000)
        data[29:33] = struct.pack(">I", zlib.crc32(memoryview(data)[12:29]))
        with mock.patch("PIL.Image.open", side_effect=AssertionError("Decoder must not open")):
            with self.assertRaisesRegex(m.MergeError, "Expected"):
                m.decode(bytes(data), SIZE, 4 * m.MIB)
        crc = bytearray(png(image((1, 2, 3, 128))))
        crc[-1] ^= 1
        with self.assertRaisesRegex(m.MergeError, "CRC"):
            m.decode(bytes(crc), SIZE, 4 * m.MIB)
        animation = io.BytesIO()
        image((1, 2, 3, 128)).save(animation, "PNG", save_all=True, append_images=[image((4, 5, 6, 128))])
        with self.assertRaisesRegex(m.MergeError, "Animated PNG"):
            m.decode(animation.getvalue(), SIZE, 4 * m.MIB)
        with self.assertRaisesRegex(m.MergeError, "16-bit"):
            m.decode(png(Image.new("I;16", (SIZE, SIZE), 40000)), SIZE, 4 * m.MIB)
        a = self.root / "mislabeled.mbtiles"
        fixture(a, [(2, 0, 0, png(image((1, 2, 3, 128))))], {"format": "webp"})
        result = self.cli("--input", a, "--output-format", "png", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disagrees", result.stderr)
        self.assertEqual(all_tiles(self.out), {})

    def test_png_compression_and_output_format_are_frozen_on_resume(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(2, 0, 0, png(image((1, 2, 3, 128))))], {"format": "png"})
        self.cli("--input", a, "--output-format", "png", "--png-compress-level", "1")
        old = all_tiles(self.out)
        for extra in (("--output-format", "webp"), ("--png-compress-level", "9")):
            result = self.cli("--input", a, *extra, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Resume requires", result.stderr)
            self.assertEqual(all_tiles(self.out), old)
        self.cli("--input", a, "--png-compress-level", "1")  # infers PNG output
        self.assertEqual(all_tiles(self.out), old)

    def test_legacy_ledger_is_refused_without_reinterpretation_or_file_changes(self):
        a, b = self.root / "a.mbtiles", self.root / "b.mbtiles"
        red, blue = image((240, 0, 0, 160)), image((0, 0, 240, 128))
        fixture(a, [(2, 0, 0, webp(red))], {"format": "webp"})
        fixture(b, [(2, 0, 0, png(blue))], {"format": "png"})
        self.cli("--input", a)
        with sqlite3.connect(self.out) as db:
            settings = json.loads(db.execute("SELECT settings FROM _merge_config").fetchone()[0])
            # Reconstruct the actual schema-1 settings shape, rather than
            # merely lowering the version on a modern-format configuration.
            del settings["output_format"], settings["png_compress_level"]
            db.execute("UPDATE _merge_config SET version=1,settings=?", (json.dumps(settings),))
        for version in (1, 2):
            with sqlite3.connect(self.out) as db:
                db.execute("UPDATE _merge_config SET version=?", (version,))
            before = self.out.read_bytes()
            rejected = self.cli("--input", b, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("NEW output", rejected.stderr)
            self.assertEqual(self.out.read_bytes(), before)
            self.pixels_equal(all_tiles(self.out)[(2, 0, 0)], red)

    def test_unique_transcodes_reserve_output_bytes_and_enforce_blob_limit(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(4, x, 0, png(image((x, 2, 3, 100)))) for x in range(10)], {"format": "png"})
        proc = self.cli("--input", a, "--batch-size", "100", "--max-tile-mib", "1", "--batch-mib", "3")
        stats = json.loads(proc.stderr.splitlines()[-1])
        self.assertGreaterEqual(stats["batches"], 5)
        self.assertLessEqual(stats["peak_batch_bytes"], 3 * m.MIB)
        # A small WebP can produce a larger PNG. Exercise the actual encoder
        # against a cap that fits the input but cannot fit its converted output.
        compact = webp(image((1, 2, 3, 100)))
        self.assertGreater(len(png(rgba(compact))), len(compact))
        with self.assertRaisesRegex(m.MergeError, "exceeds"):
            m.process_tile((1, 0, 0, compact, None), SIZE, 4, len(compact), output_format="png")

    def test_verify_rejects_wrong_output_encoding(self):
        a = self.root / "a.mbtiles"
        fixture(a, [(2, 0, 0, png(image((1, 2, 3, 100))))], {"format": "png"})
        self.cli("--input", a, "--output-format", "png")
        with sqlite3.connect(self.out) as db:
            db.execute("UPDATE tiles SET tile_data=?", (webp(image((1, 2, 3, 100))),))
        result = subprocess.run([sys.executable, str(ROOT / 'merge_mbtiles.py'), 'verify', '--output', str(self.out), '--decode'], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disagrees", result.stderr)


if __name__ == "__main__":
    unittest.main()
