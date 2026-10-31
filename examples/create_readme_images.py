#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render portable README illustrations from the actual synthetic demo images."""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

INK = "#152938"
MUTED = "#526579"
RED = "#d74132"
BLUE = "#1e64dc"
BACKGROUND = "#f3f6fa"


def font(size, bold=False):
    """Use an available font on Linux/macOS, with a portable Pillow fallback."""
    candidates = (["DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if bold else
                  ["DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"])
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def centered(draw, x, y, text, size=24, fill=INK, bold=False):
    """Position a caption by its top-center anchor, independent of text width."""
    draw.text((x, y), text, font=font(size, bold), fill=fill, anchor="mt")


def checker(size):
    """Build an opaque checkerboard so transparent pixels remain visible."""
    im = Image.new("RGBA", (size, size), "#ffffff")
    d = ImageDraw.Draw(im)
    cell = max(8, size // 16)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2:
                d.rectangle((x, y, min(x + cell - 1, size - 1), min(y + cell - 1, size - 1)), fill="#e2e8f0")
    return im


def compositing(demo, dest):
    """Render actual source/expected pixels; only their display size changes."""
    canvas = Image.new("RGB", (1560, 690), BACKGROUND)
    d = ImageDraw.Draw(canvas)
    centered(d, 780, 24, "Two raster fragments. One shared tile coordinate.", 34, bold=True)
    centered(d, 780, 72, "z=2 / x=1 / y=2  (TMS)  |  checkerboard = transparency", 23, MUTED)
    cards = [(40, "Source A", "Earlier input", "a.png"),
             (580, "Source B", "Later input", "b.png"),
             (1120, "Merged tile", "A + B, with alpha compositing", "expected_overlap.png")]
    for x, title, note, filename in cards:
        d.rounded_rectangle((x, 124, x + 400, 638), 18, fill="white", outline="#d2dce8", width=2)
        centered(d, x + 200, 145, title, 28, bold=True)
        with Image.open(demo / filename) as im:
            tile = checker(352)
            tile.alpha_composite(im.convert("RGBA").resize((352, 352), Image.Resampling.LANCZOS))
            canvas.paste(tile.convert("RGB"), (x + 24, 194))
        d.rectangle((x + 24, 194, x + 375, 545), outline="#a9b8cb", width=2)
        centered(d, x + 200, 573, note, 20, MUTED)
        centered(d, x + 200, 605, "512 x 512 pixels", 18, MUTED)
    centered(d, 510, 338, "+", 48, bold=True)
    centered(d, 1050, 338, "=", 48, bold=True)
    canvas.save(dest / "compositing.png", optimize=True)


def tile_grid(dest):
    """Illustrate coordinate union separately from same-coordinate compositing.

    This schematic is not a screenshot of the demo database: it adds unique
    x=0/x=2 cells to explain why two input archives can produce three tiles.
    """
    canvas = Image.new("RGB", (1560, 700), BACKGROUND)
    d = ImageDraw.Draw(canvas)
    centered(d, 780, 24, "Merge archives AND compose their shared tiles", 34, bold=True)
    centered(d, 780, 72, "Unique coordinates are retained; neighboring fragments at the same coordinate become one tile.", 22, MUTED)
    panels = [(40, "Source A MBTiles", "a"), (580, "Source B MBTiles", "b"), (1120, "Merged MBTiles", "merged")]
    cell = 112
    for px, title, role in panels:
        d.rounded_rectangle((px, 126, px + 400, 570), 18, fill="white", outline="#d2dce8", width=2)
        centered(d, px + 200, 147, title, 26, bold=True)
        for i in range(3):
            x, y = px + 32 + i * cell, 255
            present = (role == "a" and i in (0, 1)) or (role == "b" and i in (1, 2)) or role == "merged"
            tile = checker(cell) if present else Image.new("RGBA", (cell, cell), "#f8fafc")
            td = ImageDraw.Draw(tile)
            if present:
                if i == 0:
                    td.rectangle((0, 0, cell, cell), fill=RED)
                elif i == 2:
                    td.rectangle((0, 0, cell, cell), fill=BLUE)
                else:
                    if role in ("a", "merged"):
                        td.polygon(((0, 0), (72, 0), (49, 55), (72, cell), (0, cell)), fill=RED)
                    if role in ("b", "merged"):
                        td.polygon(((72, 0), (cell, 0), (cell, cell), (72, cell), (49, 55)), fill=BLUE)
            canvas.paste(tile.convert("RGB"), (x, y))
            d.rectangle((x, y, x + cell - 1, y + cell - 1), outline=INK if i == 1 else "#9caec3", width=3 if i == 1 else 2)
            centered(d, x + cell / 2, y + cell + 13, f"x={i}", 19)
            if not present:
                centered(d, x + cell / 2, y + 42, "absent", 18, MUTED)
        centered(d, px + 200, 425, "z=2, y=2 (TMS)", 22, MUTED)
        centered(d, px + 200, 466, "Shared coordinate: x=1", 22, bold=True)
        centered(d, px + 200, 508, "2 tiles" if role != "merged" else "3 tiles, including one composed tile", 18, MUTED)
    centered(d, 510, 293, "+", 48, bold=True)
    centered(d, 1050, 293, "=", 48, bold=True)
    centered(d, 780, 615, "Different z/x/y values stay separate. This does not stitch distinct coordinates into one oversized image.", 22, MUTED)
    canvas.save(dest / "tile-grid.png", optimize=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo-dir", type=Path, default=Path("demo"))
    p.add_argument("--output-dir", type=Path, default=Path("docs/images"))
    args = p.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    compositing(args.demo_dir, args.output_dir)
    tile_grid(args.output_dir)
