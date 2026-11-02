#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate small, synthetic 512 px WebP or PNG inputs plus the expected overlap PNG."""
import argparse
from contextlib import closing
import io
from pathlib import Path
import sqlite3
from PIL import Image, ImageDraw


def create(root, tile_format="webp"):
    """Create two independent source fragments and their expected RGBA result.

    The expected image uses Pillow directly, not merger code, so it can catch
    reversed overlay order or lost transparency in a manual end-to-end check.
    Refuse existing fixture files rather than modifying a dataset in place.
    """
    root.mkdir(parents=True, exist_ok=True)
    names = ("source_a.mbtiles", "source_b.mbtiles", "a.png", "b.png", "expected_overlap.png")
    if any((root / n).exists() for n in names):
        raise SystemExit("Demo files already exist; choose a new directory")
    # The polygons border one another, with a narrow translucent overlap.
    # They share one z/x/y; they are image fragments, not different map tiles.
    a = Image.new("RGBA", (512, 512))
    b = Image.new("RGBA", (512, 512))
    ImageDraw.Draw(a).polygon(((25, 25), (285, 25), (255, 180), (295, 300), (265, 480), (25, 480)), fill=(215, 65, 50, 210))
    ImageDraw.Draw(b).polygon(((245, 25), (485, 25), (485, 480), (225, 480), (255, 300), (215, 180)), fill=(30, 100, 220, 150))
    for label, im, bounds in (("a", a, "-90,0,0,66.513260443"), ("b", b, "-90,0,0,66.513260443")):
        buf = io.BytesIO()
        if tile_format == "png":
            im.save(buf, "PNG", compress_level=6)
        else:
            im.save(buf, "WEBP", lossless=True, exact=True)
        # Flat TMS schema and declared format match the CLI input contract.
        # The geographic bounds are synthetic and contain no local source paths.
        with closing(sqlite3.connect(root / f"source_{label}.mbtiles")) as db:
            db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB,PRIMARY KEY(zoom_level,tile_column,tile_row))")
            db.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO tiles VALUES(2,1,2,?)", (buf.getvalue(),))
            db.executemany("INSERT INTO metadata VALUES(?,?)", {"format": tile_format, "scheme": "tms", "bounds": bounds, "attribution": f"Synthetic source {label.upper()}"}.items())
            db.commit()
        im.save(root / f"{label}.png")
    Image.alpha_composite(a, b).save(root / "expected_overlap.png")
    print(root.resolve())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--tile-format", choices=("webp", "png"), default="webp")
    args = p.parse_args()
    create(args.output_dir, args.tile_format)
