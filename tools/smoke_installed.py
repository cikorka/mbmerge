#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the installed wheel from outside the checkout, including spawned workers.

Run using the target environment: python -I tools/smoke_installed.py
An editable install is intentionally rejected; this verifies distribution contents.
"""
import importlib.metadata
from contextlib import closing
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib

from PIL import Image, ImageDraw
import mbmerge
import merge_mbtiles

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fixture(path, im, fmt):
    """Create mixed-format 512px sources with one shared and one unique key."""
    buf = io.BytesIO()
    if fmt == "png":
        im.save(buf, "PNG")
    else:
        im.save(buf, "WEBP", lossless=True, exact=True)
    unique = 0 if fmt == "png" else 2
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB,PRIMARY KEY(zoom_level,tile_column,tile_row))")
        db.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)")
        db.executemany("INSERT INTO tiles VALUES(2,?,2,?)", ((x, buf.getvalue()) for x in (1, unique)))
        db.executemany("INSERT INTO metadata VALUES(?,?)", {"format": fmt, "scheme": "tms"}.items())
        db.commit()
    return buf.getvalue()


def run(command, cwd):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, result.stderr)
    return result


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = importlib.metadata.version(project["name"])
    require(version == merge_mbtiles.VERSION == mbmerge.__version__, "Installed version mismatch")
    # A checkout on sys.path could hide a wheel missing its actual implementation.
    for module in (merge_mbtiles, mbmerge):
        location = Path(module.__file__).resolve()
        require("site-packages" in location.parts or "dist-packages" in location.parts,
                f"Imported checkout instead of wheel: {location}")
    executable = shutil.which("mbmerge")
    require(executable is not None, "Installed mbmerge command is missing from PATH")
    with tempfile.TemporaryDirectory(prefix="mbmerge-wheel-") as tmp:
        work = Path(tmp)
        require(run([executable, "--version"], work).stdout.strip() == version, "CLI version mismatch")
        require(run([sys.executable, "-I", "-m", "mbmerge", "--version"], work).stdout.strip() == version,
                "python -m mbmerge version mismatch")
        # The GDAL-backed subcommand must ship in the wheel even where GDAL itself is absent.
        require("--binary-alpha" in run([executable, "tile", "--help"], work).stdout, "mbmerge tile is missing")
        a, b = Image.new("RGBA", (512, 512)), Image.new("RGBA", (512, 512))
        ImageDraw.Draw(a).rectangle((0, 0, 300, 511), fill=(220, 10, 20, 160))
        ImageDraw.Draw(b).rectangle((250, 0, 511, 511), fill=(10, 20, 220, 128))
        source_a, source_b = work / "a.mbtiles", work / "b.mbtiles"
        original_a, original_b = fixture(source_a, a, "png"), fixture(source_b, b, "webp")
        expected = Image.alpha_composite(a, b).tobytes()
        for fmt in ("png", "webp"):
            output = work / f"{fmt}.mbtiles"
            command = [executable, "merge", "--input", str(source_a), "--input", str(source_b),
                       "--output", str(output), "--workers", "2", "--batch-size", "2", "--json-log"]
            run(command + ["--output-format", fmt], work)
            with sqlite3.connect(output) as db:
                tiles = {x: data for x, data in db.execute("SELECT tile_column,tile_data FROM tiles WHERE zoom_level=2")}
                count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
                meta = dict(db.execute("SELECT * FROM metadata"))
                require(meta["format"] == fmt, "Wrong output metadata format")
                require((meta["minzoom"], meta["maxzoom"], meta["mbmerge:phase"]) == ("0", "2", "complete"),
                        "Pyramid is missing or incomplete")
                require(db.execute("SELECT count(*) FROM _merge_dirty").fetchone()[0] == 0, "Dirty parents remain")
                parent_data = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=1 AND tile_column=0 AND tile_row=1").fetchone()[0]
            require(set(tiles) == {0, 1, 2}, "Tile coordinate union is incorrect")
            for data in tiles.values():
                with Image.open(io.BytesIO(data)) as im:
                    require(im.format.lower() == fmt, "Output mixes image formats")
            with Image.open(io.BytesIO(tiles[1])) as im:
                require(im.convert("RGBA").tobytes() == expected, "Alpha compositing changed pixels")
            require(tiles[0] == original_a if fmt == "png" else tiles[2] == original_b,
                    "Unique matching-format tile was recompressed")
            with Image.open(io.BytesIO(parent_data)) as parent:
                # z=2 / x=0,1 / TMS y=2 are the SOUTH half of parent 1/0/1.
                require(parent.convert("RGBA").getpixel((0, 0)) == (0, 0, 0, 0), "TMS north/south was flipped")
            require('"skipped": 2' in run(command, work).stderr, "Completed inputs were not skipped")
            check = run([executable, "verify", "--output", str(output), "--decode", "--full-sqlite-check"], work)
            require(json.loads(check.stdout)["decoded_tiles"] == count == 6, "Full pyramid verification failed")
    print(json.dumps({"distribution": project["name"], "version": version,
                      "installed_wheel": "ok", "png_and_webp": "ok", "resume": "ok", "pyramid": "ok"}))


if __name__ == "__main__":
    main()
