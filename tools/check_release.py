#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate release names/versions and distribution contents without importing the CLI."""
import argparse
import ast
from email import message_from_bytes
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {"demo/source_a.mbtiles", "demo/source_b.mbtiles",
            "demo/png/source_a.mbtiles", "demo/png/source_b.mbtiles"}


def ensure(condition, message):
    """Release validation must also run when Python assertions are disabled."""
    if not condition:
        raise ValueError(message)


def source_version(root):
    """Read the single literal VERSION assignment; avoid build-time CLI imports."""
    tree = ast.parse((root / "merge_mbtiles.py").read_text(encoding="utf-8"))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)]
    ensure(len(values) == 1, "Core must define exactly one VERSION assignment")
    version = ast.literal_eval(values[0])
    ensure(isinstance(version, str) and version, "VERSION must be a nonempty literal string")
    return version


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def check_metadata(raw, name, version):
    """Check build metadata against the source of truth used by --version."""
    meta = message_from_bytes(raw)
    ensure(normalized(meta["Name"]) == normalized(name), "Distribution name differs from pyproject.toml")
    ensure(meta["Version"] == version, "Distribution version differs from core VERSION")
    ensure(meta["License-Expression"] == "MIT", "Distribution must declare the MIT SPDX expression")
    ensure(meta["Requires-Python"] == ">=3.11", "Requires-Python differs from the supported floor")
    ensure(meta["Description-Content-Type"] == "text/markdown", "Missing Markdown PyPI description")
    ensure(any(re.match(r"(?i)^pillow(?:\W|$)", req) for req in meta.get_all("Requires-Dist", [])),
           "Pillow runtime dependency is missing")


def check_archives(root, dist_dir, tag=""):
    """Require one wheel and one sdist, with matching identity and safe contents."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], source_version(root)
    if tag:
        ensure(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version),
               "Production release must use a stable X.Y.Z version")
        ensure(tag == f"v{version}", f"Tag must be v{version}, got {tag!r}")
    files = sorted(dist_dir.glob("*"))
    wheels, sdists = list(dist_dir.glob("*.whl")), list(dist_dir.glob("*.tar.gz"))
    ensure(len(wheels) == len(sdists) == 1 and len(files) == 2,
           "dist directory must contain exactly one wheel and one .tar.gz sdist")
    with zipfile.ZipFile(wheels[0]) as wheel:
        ensure(wheel.testzip() is None, "Wheel CRC check failed")
        names = wheel.namelist()
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        ensure(len(metadata) == 1, "Wheel must contain exactly one METADATA file")
        check_metadata(wheel.read(metadata[0]), name, version)
        modules = {n for n in names if ".dist-info/" not in n and not n.endswith("/")}
        ensure(modules == {"merge_mbtiles.py", "tile_mbtiles.py", "mbmerge.py"}, "Unexpected wheel modules/data")
        entry_file = metadata[0].rsplit("/", 1)[0] + "/entry_points.txt"
        ensure("mbmerge = merge_mbtiles:main" in wheel.read(entry_file).decode(), "mbmerge entry point is missing")
        ensure(any(n.endswith("/licenses/LICENSE.md") for n in names), "MIT license missing from wheel")
    with tarfile.open(sdists[0], "r:gz") as sdist:
        members = sdist.getmembers()
        metadata = [m for m in members if len(PurePosixPath(m.name).parts) == 2 and m.name.endswith("/PKG-INFO")]
        ensure(len(metadata) == 1, "sdist must contain one top-level PKG-INFO")
        check_metadata(sdist.extractfile(metadata[0]).read(), name, version)
        for member in members:
            path = PurePosixPath(member.name)
            ensure(not path.is_absolute() and ".." not in path.parts and not member.issym() and not member.islnk(),
                   "Unsafe path/link in source distribution")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative.endswith(".mbtiles"):
                ensure(relative in FIXTURES, f"Unapproved database in sdist: {relative}")
            ensure(not any(part in {"__pycache__", ".git", ".venv", "dist", "build", "work"} for part in path.parts[1:]),
                   f"Generated/local directory in sdist: {relative}")
            ensure(not path.name.endswith(("-wal", "-shm", "-journal", ".merge.lock")) and not path.name.startswith(".env"),
                   f"Runtime/credential file in sdist: {relative}")
    return {"name": name, "version": version, "tag": tag or None,
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag", default="", help="optional release tag, exactly v<VERSION>")
    args = parser.parse_args()
    try:
        print(json.dumps(check_archives(ROOT, args.dist_dir, args.tag), indent=2))
    except (ValueError, OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Release check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
