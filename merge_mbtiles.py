#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded, resumable lossless raster MBTiles merger and pyramid builder."""

# Incrementally composite flat PNG/WebP MBTiles with bounded working data.
#
# Execution path: main -> merge -> import_source -> build_pyramid.
# Both image phases use byte-bounded reads and bounded_results backpressure.
# Only the parent process owns SQLite connections. Spawned workers receive one
# tile job at a time and return encoded bytes; they never access a database.
#
# Three invariants are essential when changing this module:
# 1. Finish one source before starting the next: alpha compositing is ordered.
# 2. Commit pixels, metadata and the last source key together: repeating an
#    already committed translucent overlay changes its appearance.
# 3. Bound both encoded bytes and submitted jobs: a row limit alone does not
#    constrain image buffers, encoder results or executor queues.
# 4. Commit dirty-key propagation with pixels. Rebuild parents from current
#    children, highest dirty level first, and mark complete only after draining.
#
# Source snapshots and SHA-256 checks protect the history recorded in the output.
# Changed sources cannot simply be overlaid again, because removed pixels would
# survive. See ARCHITECTURE.md for the state machine and maintenance checklist;
# README.md documents supported formats, operational limits and CLI examples.
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
import hashlib
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import struct
import sys
import time
import zlib

# Program and on-disk schema versions are independent. Bump the schema when
# older clients must refuse outputs they can no longer interpret safely.
VERSION = "2.0.0"
SCHEMA_VERSION = 3
MIB = 1024 * 1024
MERCATOR_LAT = 85.0511287798066
KEY = "zoom_level,tile_column,tile_row"
UPSERT = """INSERT INTO tiles VALUES (?,?,?,?)
 ON CONFLICT(zoom_level,tile_column,tile_row)
 DO UPDATE SET tile_data=excluded.tile_data"""
# Stored TMS rows increase northwards; image rows increase downwards.
CHILD_OFFSETS = ((0, 0), (1, 0), (0, 1), (1, 1))


class MergeError(Exception):
    """Expected validation/operational error, reported as CLI exit code 1."""


class SourceChanged(MergeError):
    """Snapshot became unstable; import_source taints an affected active import."""


def log(args, event, **fields):
    """Write progress to stderr, leaving stdout free for status/verify JSON."""
    fields = {k: str(v) if isinstance(v, Path) else v for k, v in fields.items()}
    record = {"event": event, **fields}
    if getattr(args, "json_log", False):
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
    else:
        print(event + ": " + " ".join(f"{k}={v}" for k, v in fields.items()),
              file=sys.stderr, flush=True)


def token(path):
    """Return cheap identity/change markers; the content hash is authoritative.

    ctime also changes for metadata-only edits. This deliberately errs on the
    side of stopping an unstable import; restoring mtime cannot hide an edit.
    """
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def assert_static(path, expected):
    """Reject replacement, edits or live journals at an import boundary."""
    try:
        actual = token(path)
        if actual != expected:
            fields = ("device", "inode", "size", "mtime_ns", "ctime_ns")
            changed = ",".join(name for name, old, new in zip(fields, expected, actual) if old != new)
            raise SourceChanged(f"Source changed while reading ({changed}): {path}")
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise SourceChanged(f"Live SQLite sidecar {sidecar}; use an offline input")
    except OSError as e:
        raise SourceChanged(f"Source disappeared or is inaccessible: {path}: {e}") from e


def digest(path, expected):
    """Hash the whole file in fixed 1 MiB reads, checking stability on both ends."""
    assert_static(path, expected)
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(MIB):
            h.update(block)
    assert_static(path, expected)
    return h.hexdigest()


def connection_limits(db, cache_mib, max_blob):
    """Bound connection buffers without claiming a hard limit on process RSS."""
    # Negative cache_size is KiB, not pages. Disable mmap so large database
    # mappings do not bypass the explicit cache; allow temp data to spill.
    db.execute(f"PRAGMA cache_size=-{cache_mib * 1024}")
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA temp_store=FILE")
    # SQLite's length limit also applies to complete encoded rows. Leave room
    # for key/record overhead; the stricter blob check occurs before fetching.
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_blob + 65536)


@contextmanager
def source(path, args):
    """Yield (read-only connection, stat token) under one shared read snapshot.

    A BEGIN alone does not acquire a shared lock: the following schema read
    does. Do not replace this with immutable=1, which would bypass locking.
    WAL inputs are rejected because hashing the main file would miss WAL data.
    """
    before = token(path)
    assert_static(path, before)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                         isolation_level=None, timeout=args.busy_timeout)
    try:
        connection_limits(db, args.source_cache_mib, args.max_tile_mib * MIB)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.execute("SELECT count(*) FROM sqlite_schema").fetchone()  # acquire read lock
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            raise MergeError(f"WAL input is unsupported: {path}. Checkpoint it offline and set journal_mode=DELETE.")
        assert_static(path, before)
        yield db, before
    finally:
        db.close()


def validate_schema(db, path):
    """Require a flat table and an index usable by ordered keyset pagination.

    Uniqueness makes each job independent within a source. ASC/BINARY ordering
    prevents a hidden large sort instead of an indexed range read.
    """
    obj = db.execute("SELECT type FROM sqlite_schema WHERE name='tiles'").fetchone()
    if not obj or obj[0] != "table":
        raise MergeError(f"{path}: expected a flat tiles TABLE (normalized/views must be exported first)")
    cols = {r[1] for r in db.execute("PRAGMA table_info(tiles)")}
    if not {"zoom_level", "tile_column", "tile_row", "tile_data"} <= cols:
        raise MergeError(f"{path}: missing required tiles columns")
    indexed = False
    for idx in db.execute("PRAGMA index_list(tiles)").fetchall():
        if idx[2] and not idx[4]:  # unique, not partial
            quoted = idx[1].replace('"', '""')
            names = [r[2] for r in db.execute(f'PRAGMA index_info("{quoted}")')]
            details = [r for r in db.execute(f'PRAGMA index_xinfo("{quoted}")') if r[5]]
            if names == KEY.split(",") and all(r[3] == 0 and r[4] == "BINARY" for r in details):
                indexed = True
    if not indexed:
        raise MergeError(f"{path}: need UNIQUE ASC/BINARY index on ({KEY}) in this order for bounded keyset reads")


def read_metadata(db):
    """Read only metadata used by merging; absent format means inspect bytes.

    Unknown metadata is not copied wholesale. Bound values and reject duplicate
    keys rather than silently choosing an arbitrary declaration.
    """
    obj = db.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata'").fetchone()
    if not obj:
        return {}
    rows = db.execute("""SELECT name,value FROM metadata WHERE name IN
        ('format','scheme','bounds','attribution') LIMIT 9""").fetchall()
    meta = {}
    for k, v in rows:
        if k in meta or not isinstance(v, str) or len(v.encode("utf-8")) > 65536:
            raise MergeError("Invalid/duplicate/oversized source metadata")
        meta[k] = v
    if "format" in meta:
        meta["format"] = meta["format"].lower()
        if meta["format"] not in ("webp", "png"):
            raise MergeError("Only WebP and PNG inputs are supported")
    if meta.get("scheme", "tms").lower() != "tms":
        raise MergeError("Input declares non-TMS scheme; convert it explicitly before importing")
    return meta


def parse_bounds(value):
    """Return [west, south, east, north], or None to request tile-based bounds."""
    if value is None:
        return None
    try:
        w, s, e, n = map(float, value.split(","))
        if not all(math.isfinite(v) for v in (w, s, e, n)):
            return None
        if not (-180 <= w <= 180 and -180 <= e <= 180 and
                -90 <= s <= n <= 90):
            return None
        if w > e:  # MBTiles bounds cannot express a wrapped rectangle portably
            w, e = -180.0, 180.0
        return [w, s, e, n]
    except (ValueError, TypeError):
        return None


def union(a, b):
    """Union two non-wrapping geographic rectangles; None means no extent yet."""
    if a is None:
        return b
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def tile_bounds(z, x, y):
    """Return the whole tile footprint in longitude/latitude, including alpha=0."""
    # TMS: y=0 is south; no flipping of image pixels or stored row coordinates.
    n = 1 << z
    lat = lambda row: math.degrees(math.atan(math.sinh(math.pi * (2 * row / n - 1))))
    return [x / n * 360 - 180, lat(y), (x + 1) / n * 360 - 180, lat(y + 1)]


def valid_key(z, x, y):
    """Validate integer Web Mercator TMS coordinates before image processing."""
    return (all(type(v) is int for v in (z, x, y)) and
            0 <= z <= 30 and 0 <= x < (1 << z) and 0 <= y < (1 << z))


def webp_dimensions(data):
    """Bound decoder allocations BEFORE Pillow opens a WebP, including VP8X."""
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MergeError("Tile is not a RIFF WebP")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise MergeError("Invalid WebP RIFF length")
    # Check both the extended canvas and actual bitstream dimensions. Trusting
    # VP8X alone could let a small advertised canvas conceal a larger decode.
    pos, dimensions, frames = 12, [], 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise MergeError("Truncated WebP chunk")
        kind = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start, end = pos + 8, pos + 8 + size
        if end + (size & 1) > len(data):
            raise MergeError("Truncated WebP payload")
        if kind in (b"ANIM", b"ANMF"):
            raise MergeError("Animated WebP is unsupported")
        if kind == b"VP8X":
            if size != 10 or data[start] & 2:
                raise MergeError("Invalid or animated VP8X")
            dimensions.append((1 + int.from_bytes(data[start + 4:start + 7], "little"),
                               1 + int.from_bytes(data[start + 7:start + 10], "little")))
        elif kind == b"VP8L":
            if size < 5 or data[start] != 0x2f:
                raise MergeError("Invalid VP8L")
            bits = struct.unpack_from("<I", data, start + 1)[0]
            dimensions.append((1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)))
            frames += 1
        elif kind == b"VP8 ":
            if size < 10 or data[start + 3:start + 6] != b"\x9d\x01\x2a":
                raise MergeError("Invalid VP8 key frame")
            w, h = struct.unpack_from("<HH", data, start + 6)
            dimensions.append((w & 0x3fff, h & 0x3fff))
            frames += 1
        pos = end + (size & 1)
    if frames != 1 or not dimensions or any(d != dimensions[0] for d in dimensions):
        raise MergeError("Invalid/inconsistent WebP dimensions")
    return dimensions[0]


def png_dimensions(data):
    """Validate static <=8-bit PNG structure/CRCs before Pillow allocates pixels."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MergeError("Invalid PNG signature")
    pos, dimensions, has_data = 8, None, False
    # CRC over a memoryview avoids copying each chunk. Pillow still performs
    # the actual decode afterward; this pass rejects unsafe dimensions first.
    view = memoryview(data)
    while pos < len(data):
        if pos + 12 > len(data):
            raise MergeError("Truncated PNG chunk")
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 8 + size
        if size > 0x7fffffff or end + 4 > len(data):
            raise MergeError("Truncated/oversized PNG payload")
        if zlib.crc32(view[pos + 4:end]) != struct.unpack_from(">I", data, end)[0]:
            raise MergeError("Invalid PNG chunk CRC")
        if dimensions is None and kind != b"IHDR":
            raise MergeError("PNG must begin with IHDR")
        if kind == b"IHDR":
            if dimensions is not None or size != 13:
                raise MergeError("Invalid/duplicate PNG IHDR")
            w, h, depth, color, compression, filtering, interlace = struct.unpack_from(">IIBBBBB", data, pos + 8)
            allowed = {0: (1, 2, 4, 8), 2: (8,), 3: (1, 2, 4, 8), 4: (8,), 6: (8,)}
            if depth not in allowed.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise MergeError("Unsupported PNG header (16-bit PNG must be converted to 8-bit first)")
            dimensions = (w, h)
        elif kind in (b"acTL", b"fcTL", b"fdAT"):
            raise MergeError("Animated PNG is unsupported")
        elif kind == b"IDAT":
            has_data = True
        elif kind == b"IEND":
            if size or not has_data or end + 4 != len(data):
                raise MergeError("Invalid PNG end/data")
            return dimensions
        pos = end + 4
    raise MergeError("Missing PNG IEND")


def image_format(data):
    """Identify supported container signatures, independently of metadata."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise MergeError("Tile must be a static WebP or PNG image")


def lossless_webp(data):
    """Inspect validated RIFF chunks, including extended WebP with a VP8L image.

    A .webp suffix, an alpha channel or quality=100 does not imply lossless RGB.
    Call only after decode() has validated the chunk structure and bitstream.
    """
    pos = 12
    while pos < len(data):
        kind = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if kind in (b"VP8 ", b"VP8L"):
            return kind == b"VP8L"
        pos += 8 + size + (size & 1)
    return False


def decode(data, tile_size, max_blob, expected_format=None):
    """Validate one static tile and return an owned, fully loaded RGBA image.

    Header checks precede Image.open so an encoded-size cap cannot be bypassed
    by advertising enormous decoded dimensions. No resizing or color-space
    conversion is attempted; callers must provide compatible display rasters.
    """
    from PIL import Image
    if not isinstance(data, bytes) or len(data) > max_blob:
        raise MergeError("Oversized or non-BLOB tile")
    actual = image_format(data)
    if expected_format is not None and actual != expected_format:
        raise MergeError(f"Tile format {actual} disagrees with declared format {expected_format}")
    dimensions = png_dimensions(data) if actual == "png" else webp_dimensions(data)
    if dimensions != (tile_size, tile_size):
        raise MergeError(f"Expected {tile_size}x{tile_size} {actual.upper()} tile")
    with Image.open(io.BytesIO(data)) as im:
        if im.format != actual.upper() or getattr(im, "n_frames", 1) != 1 or im.size != (tile_size, tile_size):
            raise MergeError("Invalid/static image required")
        return im.convert("RGBA")


def encode(im, output_format, method, png_compress_level, max_blob):
    """Encode RGBA losslessly, rejecting results above the reserved blob limit.

    Compression settings affect newly encoded tiles only. The raw-byte copy
    path in process_tile deliberately preserves existing encoded input bytes.
    """
    out = io.BytesIO()
    if output_format == "png":
        im.save(out, "PNG", compress_level=png_compress_level)
    else:
        im.save(out, "WEBP", lossless=True, exact=True, method=method)
    encoded = out.getvalue()
    if len(encoded) > max_blob:
        raise MergeError("Encoded output exceeds --max-tile-mib")
    return encoded


def process_tile(job, tile_size, method, max_blob, output_format="webp",
                 png_compress_level=6, source_format=None):
    """Worker entry: (z,x,y,new_bytes,old_bytes_or_None) -> (z,x,y,result_bytes).

    The existing output is the base; this source is the overlay. A unique tile
    can be copied only after validation and only in the selected output format.
    All other paths encode the same 8-bit RGBA compositing result losslessly.
    """
    from PIL import Image
    z, x, y, data, old = job
    try:
        with decode(data, tile_size, max_blob, source_format) as overlay:
            if old is None:
                if image_format(data) == output_format and (output_format == "png" or lossless_webp(data)):
                    return z, x, y, data  # validate; preserve bytes in the selected format
                return z, x, y, encode(overlay, output_format, method, png_compress_level, max_blob)
            with decode(old, tile_size, max_blob, output_format) as base:
                if output_format == "webp" and not lossless_webp(old):
                    raise MergeError("Managed output contains lossy WebP; rebuild or restore it")
                with Image.alpha_composite(base, overlay) as result:
                    return z, x, y, encode(result, output_format, method, png_compress_level, max_blob)
    except Exception as e:
        raise MergeError(f"Tile {z}/{x}/{y}: {e}") from e


def worker_init():
    """Keep graceful signal handling and SQLite rollback in the parent."""
    # Parent handles graceful Ctrl-C/SIGTERM and transaction rollback.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def process_overview(job, tile_size, method, max_blob, output_format="webp",
                     png_compress_level=6, source_format=None):
    """Build one parent from four CURRENT children, never overlay an old parent.

    BOX is a 2x2 area average without a filter halo, so four children suffice.
    Premultiplied alpha prevents invisible RGB from bleeding into visible edges.
    Work is bounded by one 2S x 2S canvas and the codec's temporary buffers.
    """
    from PIL import Image
    z, x, y, children = job
    try:
        with Image.new("RGBA", (tile_size * 2, tile_size * 2)) as canvas:
            for (dx, dy), data in zip(CHILD_OFFSETS, children):
                if data is None:
                    continue  # missing quadrant remains transparent, not black/opaque
                with decode(data, tile_size, max_blob, output_format) as child:
                    if output_format == "webp" and not lossless_webp(data):
                        raise MergeError("Managed output contains lossy WebP")
                    canvas.paste(child, (dx * tile_size, (1 - dy) * tile_size))
            with canvas.convert("RGBa") as premultiplied:
                with premultiplied.resize((tile_size, tile_size), Image.Resampling.BOX) as reduced:
                    with reduced.convert("RGBA") as result:
                        return z, x, y, encode(result, output_format, method, png_compress_level, max_blob)
    except Exception as e:
        raise MergeError(f"Overview {z}/{x}/{y}: {e}") from e


def bounded_results(pool, jobs, args, metrics, source_format=None, worker=process_tile):
    """Yield unordered results while bounding all submitted/unconsumed futures.

    Jobs have distinct keys within one source, so completion order does not
    affect pixels. Source-to-source order is enforced by merge, outside the pool.
    Refill only after yielding results: a slow writer must slow the producers.
    """
    it, pending = iter(jobs), set()
    def fill():
        while len(pending) < args.max_inflight:
            try:
                job = next(it)
            except StopIteration:
                break
            pending.add(pool.submit(worker, job, args.tile_size,
                                    args.webp_method, args.max_tile_mib * MIB,
                                    args.output_format, args.png_compress_level, source_format))
        metrics["peak_inflight"] = max(metrics["peak_inflight"], len(pending))
    try:
        fill()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                pending.remove(f)
                yield f.result()
            # Drop references to completed encoded blobs BEFORE submitting more.
            done.clear()
            del f
            fill()
    finally:
        for f in pending:
            f.cancel()


@contextmanager
def output_lock(path):
    """Exclude cooperating merger instances for the entire run, not one batch."""
    if os.name != "posix":
        raise MergeError("This release supports Linux/macOS (POSIX advisory file locks)")
    import fcntl
    # Never unlink: unlinking a held lock permits concurrent locks on two inodes.
    with Path(str(path) + ".merge.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise MergeError(f"Another merger holds the output lock: {path}") from e
        yield


def _commit(db):
    """Separate function also lets tests kill the process at transaction boundaries."""
    db.commit()


def checkpoint(db):
    """Truncate WAL after commit, or stop if a reader prevents bounded reuse.

    Failure here does not roll back the preceding commit. Its persisted cursor
    must be respected on resume; replaying that batch would double its overlay.
    """
    busy, frames, done = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy or (frames > 0 and frames != done):
        raise MergeError("WAL checkpoint blocked by another reader; close output readers and resume. Latest batch is committed.")


def managed_objects(db):
    """Inspect ownership/schema without changing pragmas or starting a writer."""
    objects = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
    if objects and "_merge_config" not in objects:
        raise MergeError("Output is an unmanaged database. Use a NEW output and pass the old MBTiles as the first --input.")
    if objects:
        row = db.execute("SELECT version FROM _merge_config WHERE id=1").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            raise MergeError("Unsupported output schema version. Version 2 requires a NEW output; rebuild from original inputs. Existing archives are not modified.")
    return objects


def open_output(path, args):
    """Create/open a managed archive and resolve immutable encoding settings.

    Never adopt arbitrary pre-existing tiles or reinterpret a v1 ledger as a
    pyramid. Resolve immutable mode, grid and encoding settings for the pipeline.
    """
    # Even opening and closing an otherwise unused READ-WRITE WAL connection can
    # checkpoint committed pages into the main file. Reject foreign/old archives
    # through a read-only connection before ever requesting write access.
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=args.busy_timeout)) as probe:
            managed_objects(probe)
    db = sqlite3.connect(path, isolation_level=None, timeout=args.busy_timeout)
    try:
        objects = managed_objects(db)  # recheck after reopening, under the CLI lock
        connection_limits(db, args.cache_mib, args.max_tile_mib * MIB)
        db.execute(f"PRAGMA busy_timeout={int(args.busy_timeout * 1000)}")
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise MergeError("Output filesystem does not support SQLite WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA fullfsync=ON")
        db.execute("PRAGMA checkpoint_fullfsync=ON")
        # Checkpoints are explicit after each small commit. A size-limit pragma
        # alone cannot cap WAL growth while another connection pins old pages.
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("PRAGMA journal_size_limit=0")
        if not objects:
            # Schema + initial config are one transaction, so a crash cannot
            # leave a half-created database that looks like an unmanaged input.
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tiles(zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL,
                  tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL,
                  PRIMARY KEY(zoom_level,tile_column,tile_row));
                CREATE TABLE _merge_config(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                  settings TEXT NOT NULL);
                CREATE TABLE _merge_sources(
                  seq INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                  last_z INTEGER, last_x INTEGER, last_y INTEGER,
                  tile_count INTEGER NOT NULL DEFAULT 0, overlaps INTEGER NOT NULL DEFAULT 0,
                  minzoom INTEGER, maxzoom INTEGER, west REAL, south REAL, east REAL, north REAL,
                  source_metadata TEXT NOT NULL, completed_at TEXT);
                CREATE TABLE _merge_attributions(value TEXT PRIMARY KEY);
                CREATE TABLE _merge_dirty(z INTEGER NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,
                  PRIMARY KEY(z,x,y)) WITHOUT ROWID;
                CREATE TABLE _merge_state(id INTEGER PRIMARY KEY CHECK(id=1),phase TEXT NOT NULL,
                  overviews_written INTEGER NOT NULL DEFAULT 0);
                INSERT INTO _merge_state(id,phase) VALUES(1,'empty');
            """)
            settings = {"tile_size": args.tile_size, "webp_method": args.webp_method,
                        "output_format": args.output_format or "webp",
                        "png_compress_level": args.png_compress_level,
                        "mode": args.mode or "pyramid", "base_zoom": args.base_zoom,
                        "min_zoom": args.min_zoom if args.min_zoom is not None else 0,
                        "resampling": "box-premultiplied-alpha",
                        "lossless": True,
                        "name": args.name or path.stem,
                        "description": args.description or "Incrementally composited raster tiles",
                        "type": args.type or "overlay"}
            db.execute("INSERT INTO _merge_config VALUES(1,?,?)",
                       (SCHEMA_VERSION, json.dumps(settings)))
            _commit(db)
        row = db.execute("SELECT version,settings FROM _merge_config WHERE id=1").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            raise MergeError("Unsupported output schema version")
        settings = json.loads(row[1])
        if settings.get("output_format") not in ("webp", "png"):
            raise MergeError("Invalid output format in configuration")
        if args.output_format is not None and args.output_format != settings["output_format"]:
            raise MergeError(f"Resume requires the original --output-format={settings['output_format']}; choose a NEW output to convert formats")
        args.output_format = settings["output_format"]
        if (settings.get("mode") not in ("pyramid", "overlay") or settings.get("lossless") is not True
                or settings.get("resampling") != "box-premultiplied-alpha"):
            raise MergeError("Invalid mode/lossless configuration")
        pristine = (db.execute("SELECT phase FROM _merge_state WHERE id=1").fetchone()[0] == "empty"
                    and not db.execute("SELECT 1 FROM _merge_sources LIMIT 1").fetchone())
        for k in ("mode", "base_zoom", "min_zoom"):
            requested = getattr(args, k)
            if requested is not None and requested != settings[k]:
                if pristine:
                    settings[k] = requested  # a rejected plan has imported no pixels
                else:
                    raise MergeError(f"Resume requires the original --{k.replace('_', '-')}={settings[k]}")
            setattr(args, k, settings[k])
        if args.mode == "overlay" and (args.base_zoom is not None or args.min_zoom != 0):
            raise MergeError("--base-zoom/--min-zoom apply only to pyramid mode")
        if args.mode == "pyramid" and args.batch_mib < 5 * args.max_tile_mib:
            raise MergeError("Pyramid --batch-mib must be >= 5 * --max-tile-mib (four children + parent)")
        frozen = ("tile_size", "webp_method" if args.output_format == "webp" else "png_compress_level")
        for k in frozen:
            if settings[k] != getattr(args, k):
                raise MergeError(f"Resume requires the original --{k.replace('_', '-')}={settings[k]}")
        for k in ("name", "description", "type"):
            if getattr(args, k) is not None:
                settings[k] = getattr(args, k)
        db.execute("CREATE TEMP TABLE batch_keys(z INTEGER,x INTEGER,y INTEGER,PRIMARY KEY(z,x,y)) WITHOUT ROWID")
        db.execute("CREATE TEMP TABLE child_keys(z INTEGER,x INTEGER,y INTEGER,PRIMARY KEY(z,x,y)) WITHOUT ROWID")
        checkpoint(db)
        return db, settings
    except BaseException:
        db.close()
        raise


def update_metadata(db, settings):
    """Update display metadata and schema/config inside the caller's transaction.

    Per-source statistics aggregate actual committed keys; tile_count counts
    contributions, not distinct output coordinates. Bounds may intentionally use
    a source's full declared extent before that source has completed importing.
    """
    # Scan only the small source ledger, never the TB-sized tiles table.
    w, s, e, n = db.execute("""SELECT min(west),min(south),
        max(east),max(north) FROM _merge_sources WHERE tile_count>0""").fetchone()
    # The primary key starts with zoom, so these extrema are index end-point
    # lookups, not scans over every tile. Include only actually committed levels.
    low = db.execute("SELECT zoom_level FROM tiles ORDER BY zoom_level LIMIT 1").fetchone()
    high = db.execute("SELECT zoom_level FROM tiles ORDER BY zoom_level DESC LIMIT 1").fetchone()
    z0, z1 = (low[0], high[0]) if low else (None, None)
    phase = db.execute("SELECT phase FROM _merge_state WHERE id=1").fetchone()[0]
    meta = {"name": settings["name"], "description": settings["description"],
            "type": settings["type"], "version": "1.3", "format": settings["output_format"], "scheme": "tms",
            "mbmerge:phase": phase, "mbmerge:mode": settings["mode"]}
    if z0 is not None:
        def fmt(v):
            return f"{v:.10f}".rstrip("0").rstrip(".") or "0"
        meta.update(minzoom=str(z0), maxzoom=str(z1),
                    bounds=",".join(fmt(v) for v in (w, s, e, n)),
                    center=f"{fmt((w + e) / 2)},{fmt((s + n) / 2)},{z0}")
    # Global attribution length is bounded independently of tile blobs.
    total = db.execute("SELECT coalesce(sum(length(CAST(value AS BLOB))),0) FROM _merge_attributions").fetchone()[0]
    count = db.execute("SELECT count(*) FROM _merge_attributions").fetchone()[0]
    if total + 3 * count > MIB:
        raise MergeError("Merged attributions exceed 1 MiB; shorten source attributions")
    attrs = [r[0] for r in db.execute("SELECT value FROM _merge_attributions ORDER BY value")]
    if attrs:
        meta["attribution"] = " | ".join(attrs)
    db.executemany("INSERT INTO metadata VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   meta.items())
    db.execute("UPDATE _merge_config SET version=?,settings=? WHERE id=1", (SCHEMA_VERSION, json.dumps(settings)))


def read_batch(src, db, last, args, source_format=None):
    """Read one byte-bounded batch, returning (worker_jobs, reserved_bytes).

    First fetch only keys/lengths, then look up existing lengths in one join.
    Select the prefix fitting the budget BEFORE loading any image blobs. The
    temporary key table is bounded by batch_size and uses this same writer.
    """
    clauses, params = [], ()
    if args.mode == "pyramid":
        clauses.append("zoom_level=?")
        params += (args.base_zoom,)
        if last is not None:
            if last[0] != args.base_zoom:
                raise MergeError("Invalid base-zoom resume cursor")
            # With an equality prefix, compare only the remaining index columns.
            # Including zoom again makes SQLite seek by zoom alone and scan all
            # earlier x/y rows on every batch, becoming quadratic on big inputs.
            clauses.append("(tile_column,tile_row) > (?,?)")
            params += tuple(last[1:])
    elif last is not None:
        clauses.append(f"({KEY}) > (?,?,?)")
        params += tuple(last)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    candidates = src.execute(f"SELECT {KEY},length(tile_data),typeof(tile_data) FROM tiles {where} ORDER BY {KEY} LIMIT ?",
                             params + (args.batch_size,)).fetchall()
    if not candidates:
        return [], 0
    max_blob = args.max_tile_mib * MIB
    for z, x, y, size, kind in candidates:
        if not valid_key(z, x, y) or kind != "blob" or size is None or not 0 < size <= max_blob:
            raise MergeError(f"Invalid TMS key or oversized/non-BLOB tile at {z}/{x}/{y}")
    db.execute("DELETE FROM batch_keys")
    db.executemany("INSERT INTO batch_keys VALUES(?,?,?)", (r[:3] for r in candidates))
    # CROSS JOIN fixes a small-key-table outer loop; no TB-wide destination scan.
    old_sizes = {(z, x, y): size for z, x, y, size in db.execute("""SELECT b.z,b.x,b.y,length(t.tile_data)
        FROM batch_keys b CROSS JOIN tiles t
        WHERE t.zoom_level=b.z AND t.tile_column=b.x AND t.tile_row=b.y""")}
    # Keep the prefix contiguous in source key order. Its last key is the only
    # position needed for exact resume; OFFSET would become expensive at scale.
    chosen, size_sum = [], 0
    # Reserve output bytes too. This also bounds per-transaction payload, including
    # highly compressed inputs which may produce much larger lossless composites.
    for z, x, y, size, _ in candidates:
        old_size = old_sizes.get((z, x, y), 0)
        if old_size > max_blob:
            raise MergeError("Existing output tile exceeds --max-tile-mib")
        # WebP metadata does not declare lossless/lossy RGB. Even a unique WebP
        # may require lossless transcoding, so budget its worst-case result.
        output_size = size if not old_size and source_format == args.output_format == "png" else max_blob
        cost = size + old_size + output_size
        if size_sum + cost > args.batch_mib * MIB:
            break
        chosen.append((z, x, y))
        size_sum += cost
    if not chosen:
        raise MergeError("Batch byte budget is too small for one tile pair; increase --batch-mib")
    db.execute("DELETE FROM batch_keys")
    db.executemany("INSERT INTO batch_keys VALUES(?,?,?)", chosen)
    old = {(z, x, y): data for z, x, y, data in db.execute("""SELECT b.z,b.x,b.y,t.tile_data
        FROM batch_keys b CROSS JOIN tiles t
        WHERE t.zoom_level=b.z AND t.tile_column=b.x AND t.tile_row=b.y""")}
    rows = src.execute(f"SELECT {KEY},tile_data FROM tiles {where} ORDER BY {KEY} LIMIT ?",
                       params + (len(chosen),)).fetchall()
    if [r[:3] for r in rows] != chosen:
        raise SourceChanged("Input keyset changed during read")
    jobs = [(z, x, y, data, old.get((z, x, y))) for z, x, y, data in rows]
    return jobs, size_sum


def prepare_sources(db, paths, args, settings):
    """Resolve one common base level before committing any source pixels.

    Automatic selection requires equal actual maxima, not metadata maxzoom.
    Explicit selection allows an intentional common level present in all inputs.
    Open only one input at a time; file/zoom descriptors are tiny, not tile data.
    """
    if args.mode != "pyramid":
        return
    maxima = []
    for path in paths:
        with source(path, args) as (src, stamp):
            validate_schema(src, path)
            read_metadata(src)
            low = src.execute("SELECT zoom_level FROM tiles ORDER BY zoom_level LIMIT 1").fetchone()
            high = src.execute("SELECT zoom_level FROM tiles ORDER BY zoom_level DESC LIMIT 1").fetchone()
            if high:
                if any(type(r[0]) is not int or not 0 <= r[0] <= 30 for r in (low, high)):
                    raise MergeError(f"Invalid source zoom range: {path}")
                maxima.append((path, high[0]))
                if args.base_zoom is not None and not src.execute(
                        "SELECT 1 FROM tiles WHERE zoom_level=? LIMIT 1", (args.base_zoom,)).fetchone():
                    raise MergeError(f"Input has no tiles at base zoom {args.base_zoom}: {path}")
            assert_static(path, stamp)
    if args.base_zoom is None:
        levels = {z for _, z in maxima}
        if not levels:
            raise MergeError("No nonempty input to determine --base-zoom; specify it explicitly for an empty pyramid")
        if len(levels) != 1:
            raise MergeError("Inputs have different highest zooms. Choose an explicit common --base-zoom present in every nonempty input; no sources are silently dropped or upscaled.")
        args.base_zoom = levels.pop()
    if args.min_zoom > args.base_zoom:
        raise MergeError("--min-zoom must be <= --base-zoom")
    # Inferred equal maxima necessarily exist in every nonempty input. Explicit
    # levels were checked above. Recheck in import_source's locked snapshot too.
    settings["base_zoom"] = args.base_zoom
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE _merge_config SET settings=? WHERE id=1", (json.dumps(settings),))
    _commit(db)
    checkpoint(db)


def mark_parents(db, keys, min_zoom):
    """Persist deduplicated immediate parents in the SAME transaction as pixels.

    Do not accumulate the full dirty set in Python. Higher ancestors are queued
    when their children are regenerated, which also gives crash-safe ordering.
    """
    db.executemany("INSERT OR IGNORE INTO _merge_dirty VALUES(?,?,?)",
                   ((z - 1, x // 2, y // 2) for z, x, y in keys if z > min_zoom))


def read_overview_batch(db, args):
    """Read a bounded prefix of the highest dirty level, then its child blobs.

    Length lookups precede blob reads. The temporary child key table has at
    most four times batch_size rows, and joins probe the tiles primary key.
    """
    top = db.execute("SELECT z FROM _merge_dirty ORDER BY z DESC LIMIT 1").fetchone()
    if not top:
        return [], 0
    z = top[0]
    keys = db.execute("SELECT z,x,y FROM _merge_dirty WHERE z=? ORDER BY x,y LIMIT ?",
                      (z, args.batch_size)).fetchall()
    def children(parent_keys):
        return ((tz + 1, 2 * x + dx, 2 * y + dy)
                for tz, x, y in parent_keys for dx, dy in CHILD_OFFSETS)
    db.execute("DELETE FROM child_keys")
    db.executemany("INSERT INTO child_keys VALUES(?,?,?)", children(keys))
    sizes = {}
    for tz, x, y, size, kind in db.execute("""SELECT k.z,k.x,k.y,length(t.tile_data),typeof(t.tile_data)
        FROM child_keys k CROSS JOIN tiles t
        WHERE t.zoom_level=k.z AND t.tile_column=k.x AND t.tile_row=k.y"""):
        if kind != "blob" or not 0 < size <= args.max_tile_mib * MIB:
            raise MergeError(f"Invalid/oversized child at {tz}/{x}/{y}")
        sizes[(tz, x, y)] = size
    chosen, budget = [], 0
    for key in keys:
        cost = args.max_tile_mib * MIB + sum(sizes.get(k, 0) for k in children([key]))
        if budget + cost > args.batch_mib * MIB:
            break
        chosen.append(key)
        budget += cost
    if not chosen:
        raise MergeError("Overview batch byte budget too small for four children + parent")
    db.execute("DELETE FROM child_keys")
    db.executemany("INSERT INTO child_keys VALUES(?,?,?)", children(chosen))
    blobs = {(tz, x, y): data for tz, x, y, data in db.execute("""SELECT k.z,k.x,k.y,t.tile_data
        FROM child_keys k CROSS JOIN tiles t
        WHERE t.zoom_level=k.z AND t.tile_column=k.x AND t.tile_row=k.y""")}
    jobs = [(tz, x, y, tuple(blobs.get(k) for k in children([(tz, x, y)]))) for tz, x, y in chosen]
    return jobs, budget


def build_pyramid(db, pool, args, settings, metrics):
    """Regenerate dirty parents bottom-up; each batch is an atomic checkpoint.

    A crash before commit retains the current dirty keys. A crash after commit
    retains their pixels AND queued ancestors. An old parent is never an input.
    Additional base imports can safely extend a partially generated pyramid.
    """
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE _merge_state SET phase='building' WHERE id=1")
    update_metadata(db, settings)
    _commit(db)
    checkpoint(db)
    last_log = 0.0
    while True:
        jobs, budget = read_overview_batch(db, args)
        if not jobs:
            break
        metrics["peak_batch_bytes"] = max(metrics["peak_batch_bytes"], budget)
        db.execute("BEGIN IMMEDIATE")
        try:
            rows, row_bytes = [], 0
            results = bounded_results(pool, jobs, args, metrics, worker=process_overview)
            try:
                for result in results:
                    size = len(result[3])
                    if rows and (len(rows) >= args.sql_batch_size or row_bytes + size > args.sql_batch_mib * MIB):
                        db.executemany(UPSERT, rows)
                        rows.clear()
                        row_bytes = 0
                    rows.append(result)
                    row_bytes += size
                if rows:
                    db.executemany(UPSERT, rows)
            finally:
                results.close()
            keys = [job[:3] for job in jobs]
            mark_parents(db, keys, args.min_zoom)
            db.executemany("DELETE FROM _merge_dirty WHERE z=? AND x=? AND y=?", keys)
            db.execute("UPDATE _merge_state SET overviews_written=overviews_written+? WHERE id=1", (len(keys),))
            update_metadata(db, settings)
            _commit(db)
            checkpoint(db)
            metrics["overview_tiles"] += len(jobs)
            metrics["overview_batches"] += 1
            if time.monotonic() - last_log >= args.log_interval:
                log(args, "overviews", zoom=jobs[0][0], written=metrics["overview_tiles"], batch_bytes=budget,
                    peak_inflight=metrics["peak_inflight"])
                last_log = time.monotonic()
            del jobs, rows, result, results, keys
        except BaseException:
            db.rollback()
            raise


def preflight(db, args):
    """Recheck every historical contribution before importing anything new.

    A hash mismatch here leaves the output untainted: restoring the original
    source is sufficient. A change DURING import has different safety semantics.
    """
    # Validate every previous contribution, including paths omitted from this run.
    for path_s, expected, state in db.execute("SELECT path,sha256,status FROM _merge_sources ORDER BY seq"):
        if state == "tainted":
            raise MergeError(f"Output was tainted by an input changing during import: {path_s}. Rebuild to a new output.")
        path = Path(path_s)
        log(args, "verify_source", path=path)
        with source(path, args) as (src, stamp):
            actual = digest(path, stamp)
            if actual != expected:
                raise MergeError(f"Changed imported input: {path}. Restore the original exact bytes or rebuild to a NEW output; overlay replacement is unsafe.")


def import_source(db, pool, path, args, settings, metrics):
    """Import/resume one source through atomic, individually recoverable batches.

    The source read snapshot spans this whole function; output transactions span
    one batch. Complete one source before entering this function for the next.
    """
    with source(path, args) as (src, stamp):
        validate_schema(src, path)
        meta = read_metadata(src)
        if args.mode == "pyramid" and src.execute("SELECT 1 FROM tiles LIMIT 1").fetchone():
            if not src.execute("SELECT 1 FROM tiles WHERE zoom_level=? LIMIT 1", (args.base_zoom,)).fetchone():
                raise MergeError(f"Input has no tiles at base zoom {args.base_zoom}: {path}")
        bounds = parse_bounds(meta.get("bounds"))
        if bounds is None:
            log(args, "bounds_fallback", path=path, reason="missing/invalid bounds; using whole TMS tile extents")
        sha = digest(path, stamp)
        row = db.execute("SELECT seq,sha256,status,last_z,last_x,last_y,tile_count,overlaps,minzoom,maxzoom,west,south,east,north FROM _merge_sources WHERE path=?", (str(path),)).fetchone()
        if row:
            if row[1] != sha or row[2] == "tainted":
                raise MergeError(f"Changed/tainted input {path}; rebuild to a NEW output")
            if row[2] == "complete":
                return
        else:
            # Register identity before pixels. A crash here leaves an active
            # source at key=None, which restarts from its first tile safely.
            db.execute("BEGIN IMMEDIATE")
            seq = db.execute("INSERT INTO _merge_sources(path,sha256,fingerprint,status,source_metadata) VALUES(?,?,?,'active',?)",
                             (str(path), sha, json.dumps(stamp), json.dumps(meta))).lastrowid
            db.execute("UPDATE _merge_state SET phase='importing' WHERE id=1")
            update_metadata(db, settings)
            _commit(db)
            checkpoint(db)
            row = (seq, sha, "active", None, None, None, 0, 0, None, None, None, None, None, None)
        # Tuple order mirrors the SELECT above. count/overlaps are source
        # contributions; initial last is the COMMITTED (z,x,y), not a worker cursor.
        seq, _, _, z, x, y, count, overlaps, minz, maxz, w, s, e, n = row
        last = None if z is None else (z, x, y)
        extent = None if w is None else [w, s, e, n]
        last_log = 0.0
        log(args, "import", path=path, resumed_tiles=count)
        try:
            while True:
                assert_static(path, stamp)
                jobs, budget = read_batch(src, db, last, args, meta.get("format"))
                if not jobs:
                    break
                metrics["peak_batch_bytes"] = max(metrics["peak_batch_bytes"], budget)
                batch_overlap = sum(job[4] is not None for job in jobs)
                new_extent = extent
                for tz, tx, ty, _, _ in jobs:
                    new_extent = union(new_extent, bounds or tile_bounds(tz, tx, ty))
                    minz = tz if minz is None else min(minz, tz)
                    maxz = tz if maxz is None else max(maxz, tz)
                last = jobs[-1][:3]
                # From here through _commit, output pixels, source position and
                # metadata are inseparable. Worker/SQL failures roll all back.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE _merge_state SET phase='importing' WHERE id=1")
                sql_rows, sql_bytes = [], 0
                results = bounded_results(pool, jobs, args, metrics, meta.get("format"))
                try:
                    for result in results:
                        size = len(result[3])
                        if sql_rows and (len(sql_rows) >= args.sql_batch_size or sql_bytes + size > args.sql_batch_mib * MIB):
                            db.executemany(UPSERT, sql_rows)
                            sql_rows.clear()
                            sql_bytes = 0
                        sql_rows.append(result)
                        sql_bytes += size
                    if sql_rows:
                        db.executemany(UPSERT, sql_rows)
                finally:
                    results.close()
                # Check again after CPU work, before making any new pixels
                # durable. Never advance the ledger just when a job is queued.
                assert_static(path, stamp)
                count += len(jobs)
                overlaps += batch_overlap
                extent = new_extent
                db.execute("""UPDATE _merge_sources SET last_z=?,last_x=?,last_y=?,tile_count=?,overlaps=?,
                    minzoom=?,maxzoom=?,west=?,south=?,east=?,north=? WHERE seq=?""",
                           (*last, count, overlaps, minz, maxz, *extent, seq))
                if meta.get("attribution"):
                    db.execute("INSERT OR IGNORE INTO _merge_attributions VALUES(?)", (meta["attribution"],))
                if args.mode == "pyramid":
                    mark_parents(db, (job[:3] for job in jobs), args.min_zoom)
                update_metadata(db, settings)
                _commit(db)  # pixels + keyset + metadata, atomically
                checkpoint(db)  # stop if readers prevent bounded WAL reuse
                metrics["batches"] += 1
                if time.monotonic() - last_log >= args.log_interval:
                    log(args, "progress", path=path.name, tiles=count, overlaps=overlaps,
                        batch_bytes=budget, peak_inflight=metrics["peak_inflight"])
                    last_log = time.monotonic()
                # Do not retain the previous batch while loading the next one.
                del jobs, sql_rows, result, results
            # Detect out-of-band edits with restored size/mtime as well.
            if digest(path, stamp) != sha:
                raise SourceChanged(f"Source content changed during import: {path}")
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE _merge_sources SET status='complete',completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE seq=?", (seq,))
            update_metadata(db, settings)
            _commit(db)
            checkpoint(db)
            log(args, "complete_source", path=path.name, tiles=count, overlaps=overlaps)
        except BaseException as exc:
            db.rollback()
            if isinstance(exc, SourceChanged):
                # Already committed batches may have read an unstable source.
                # Persist this in a NEW transaction after discarding the current
                # batch; deleting the flag would not repair those pixels.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE _merge_sources SET status='tainted' WHERE seq=?", (seq,))
                _commit(db)
                # Keep the taint durable even if an external reader blocks checkpoint.
            raise


def collect_inputs(args, output):
    """Resolve the public priority order, deduplicate paths and reject aliases."""
    files = [Path(p).expanduser().resolve(strict=True) for p in args.input]
    if args.input_list:
        with Path(args.input_list).expanduser().open(encoding="utf-8") as f:
            # Relative entries resolve against the list's directory, not CWD.
            root = Path(args.input_list).expanduser().resolve().parent
            files += [(root / line.strip()).resolve(strict=True) for line in f if line.strip() and not line.lstrip().startswith("#")]
    if args.input_dir:
        root = Path(args.input_dir).expanduser().resolve(strict=True)
        files += sorted(p.resolve() for p in root.glob(args.pattern) if p.is_file())
    unique = list(dict.fromkeys(files))
    identities = {}
    for path in unique:
        if path == output or (output.exists() and path.samefile(output)):
            raise MergeError(f"Output is also an input: {path}; choose a separate output path/directory")
        identity = token(path)[:2]
        if identity in identities:
            raise MergeError(f"Two input paths refer to the same physical file: {identities[identity]} and {path}")
        identities[identity] = path
    return unique


def merge(args):
    """Parent-only orchestration: lock, validate history, import sources, finalize.

    All SQLite connections stay in this process. Spawn is required even on
    systems where fork is the default, to avoid inheriting their handles.
    """
    output = Path(args.output).expanduser().resolve()
    if not output.parent.is_dir():
        raise MergeError(f"Output directory does not exist: {output.parent}")
    paths = collect_inputs(args, output)
    metrics = {"batches": 0, "peak_inflight": 0, "peak_batch_bytes": 0,
               "overview_tiles": 0, "overview_batches": 0}
    start = time.monotonic()
    with output_lock(output):
        if output.exists() and output.stat().st_nlink != 1:
            raise MergeError("Output must not have hard links (SQLite WAL and lock identity would be ambiguous)")
        db, settings = open_output(output, args)
        try:
            if args.output_format == "webp":
                from PIL import features
                if not features.check("webp"):
                    raise MergeError("Pillow has no WebP support; use --output-format png for PNG-only work")
            preflight(db, args)
            prior = dict(db.execute("SELECT path,status FROM _merge_sources ORDER BY seq"))
            prior_identities = {token(Path(p))[:2]: p for p in prior}
            for path in paths:
                alias = prior_identities.get(token(path)[:2])
                if alias is not None and alias != str(path):
                    raise MergeError(f"New input aliases an already imported physical file: {alias}")
            # A partially imported source always finishes before any new overlay.
            active = [Path(p) for p, state in prior.items() if state == "active"]
            todo = active + [p for p in paths if str(p) not in prior]
            if not paths and not active and not prior:
                raise MergeError("No inputs found")
            prepare_sources(db, todo, args, settings)
            log(args, "plan", mode=args.mode, base_zoom=args.base_zoom, min_zoom=args.min_zoom,
                new_or_active=len(todo), skipped=sum(prior.get(str(p)) == "complete" for p in paths), workers=args.workers)
            dirty = db.execute("SELECT 1 FROM _merge_dirty LIMIT 1").fetchone()
            if todo or dirty:
                with ProcessPoolExecutor(max_workers=args.workers,
                        mp_context=multiprocessing.get_context("spawn"), initializer=worker_init) as pool:
                    for path in todo:
                        import_source(db, pool, path, args, settings, metrics)
                    if args.mode == "pyramid":
                        build_pyramid(db, pool, args, settings, metrics)
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE _merge_state SET phase='complete' WHERE id=1")
            update_metadata(db, settings)
            _commit(db)
            checkpoint(db)
            log(args, "finished", seconds=round(time.monotonic() - start, 3), **metrics)
        finally:
            db.rollback()
            db.close()
    return metrics


def readonly_output(path):
    """Open an existing output without accidentally creating a missing file."""
    p = Path(path).expanduser().resolve(strict=True)
    db = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA cache_size=-8192")
    return db


def status(args):
    """Report the small ledger and metadata without scanning all tile blobs."""
    with closing(readonly_output(args.output)) as db:
        result = {"schema_version": db.execute("SELECT version FROM _merge_config WHERE id=1").fetchone()[0],
                  "settings": json.loads(db.execute("SELECT settings FROM _merge_config WHERE id=1").fetchone()[0]),
                  "phase": db.execute("SELECT phase FROM _merge_state WHERE id=1").fetchone()[0],
                  "pending_parents": db.execute("SELECT count(*) FROM _merge_dirty").fetchone()[0],
                  "overviews_written": db.execute("SELECT overviews_written FROM _merge_state WHERE id=1").fetchone()[0],
                  "sources": dict(db.execute("SELECT status,count(*) FROM _merge_sources GROUP BY status")),
                  "contributions": db.execute("SELECT coalesce(sum(tile_count),0) FROM _merge_sources").fetchone()[0],
                  "metadata": dict(db.execute("SELECT name,value FROM metadata")),
                  "unfinished": [dict(zip(("path", "status", "tiles", "last_z", "last_x", "last_y"), row))
                     for row in db.execute("SELECT path,status,tile_count,last_z,last_x,last_y FROM _merge_sources WHERE status!='complete'")]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def verify(args):
    """Explicitly scan SQLite and optionally decode every stored output tile.

    This checks storage/encoding, not geographic correctness or source hashes.
    The inexpensive status command and preflight have different responsibilities.
    """
    with closing(readonly_output(args.output)) as db:
        if any(row[0] != "ok" for row in db.execute("PRAGMA integrity_check" if args.full_sqlite_check else "PRAGMA quick_check")):
            raise MergeError("SQLite integrity check failed")
        tainted = db.execute("SELECT count(*) FROM _merge_sources WHERE status='tainted'").fetchone()[0]
        if tainted:
            raise MergeError("Output is tainted; rebuild it")
        unfinished = db.execute("SELECT count(*) FROM _merge_sources WHERE status!='complete'").fetchone()[0]
        dirty = db.execute("SELECT 1 FROM _merge_dirty LIMIT 1").fetchone()
        phase = db.execute("SELECT phase FROM _merge_state WHERE id=1").fetchone()[0]
        if unfinished or dirty or phase != "complete":
            raise MergeError("Output is incomplete (source import or pyramid pending); resume merge before verification")
        checked = 0
        if args.decode:
            settings = json.loads(db.execute("SELECT settings FROM _merge_config WHERE id=1").fetchone()[0])
            for z, x, y, size in db.execute(f"SELECT {KEY},length(tile_data) FROM tiles"):
                if not valid_key(z, x, y) or not 0 < size <= args.max_tile_mib * MIB:
                    raise MergeError(f"Invalid output tile {z}/{x}/{y}")
            for z, x, y, data in db.execute(f"SELECT {KEY},tile_data FROM tiles"):
                with decode(data, settings["tile_size"], args.max_tile_mib * MIB,
                            settings.get("output_format", "webp")):
                    if settings["output_format"] == "webp" and not lossless_webp(data):
                        raise MergeError(f"Lossy WebP in managed output at {z}/{x}/{y}")
                    checked += 1
        print(json.dumps({"sqlite": "ok", "decoded_tiles": checked, "unfinished_sources": unfinished,
                          "phase": phase,
                          "note": "Does not recheck source fingerprints; merge does that before resuming."}))


def positive(value):
    """Shared argparse validator for positive integer resource limits."""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def parser():
    """Declare CLI defaults; open_output resolves the existing output's format."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("merge", help="merge the base zoom and build overviews; append or resume safely")
    m.add_argument("--output", required=True)
    m.add_argument("--output-format", choices=("webp", "png"),
                   help="new output: webp by default; existing output: use its recorded format")
    m.add_argument("--input", action="append", default=[], help="repeatable; order is compositing priority")
    m.add_argument("--input-dir")
    m.add_argument("--pattern", default="*.mbtiles", help="glob within input-dir; ** supported")
    m.add_argument("--input-list", help="UTF-8 paths, one per line; appended after --input")
    m.add_argument("--mode", choices=("pyramid", "overlay"),
                   help="new outputs: pyramid by default; overlay preserves every supplied zoom; existing outputs infer their mode")
    m.add_argument("--base-zoom", type=int, choices=range(31), metavar="Z",
                   help="common source level; default: equal actual highest zooms; fixed on resume")
    m.add_argument("--min-zoom", type=int, choices=range(31), metavar="Z",
                   help="lowest generated level, default 0; fixed on resume")
    m.add_argument("--workers", type=positive, default=min(4, os.cpu_count() or 1))
    m.add_argument("--max-inflight", type=positive, help="default: 2 * workers")
    m.add_argument("--batch-size", type=positive, default=128, help="max source keys or overview parents per transaction")
    m.add_argument("--batch-mib", type=positive, default=32, help="encoded input + old + worst-case output byte budget")
    m.add_argument("--max-tile-mib", type=positive, default=4)
    m.add_argument("--sql-batch-size", type=positive, default=16)
    m.add_argument("--sql-batch-mib", type=positive, default=4)
    m.add_argument("--cache-mib", type=positive, default=32)
    m.add_argument("--source-cache-mib", type=positive, default=8)
    m.add_argument("--tile-size", type=positive, default=512)
    m.add_argument("--webp-method", type=int, choices=range(7), default=4)
    m.add_argument("--png-compress-level", type=int, choices=range(10), default=6,
                   help="PNG compression effort 0-9; never changes pixel quality")
    m.add_argument("--busy-timeout", type=positive, default=5, help="seconds")
    m.add_argument("--log-interval", type=positive, default=5, help="seconds; completed sources always logged")
    m.add_argument("--name")
    m.add_argument("--description")
    m.add_argument("--type", choices=("overlay", "baselayer"))
    m.add_argument("--json-log", action="store_true")
    m.set_defaults(func=merge)
    s = sub.add_parser("status", help="small ledger/metadata summary, no tile scan")
    s.add_argument("--output", required=True)
    s.set_defaults(func=status)
    v = sub.add_parser("verify", help="full SQLite quick_check; optionally decode every tile (slow)")
    v.add_argument("--output", required=True)
    v.add_argument("--full-sqlite-check", action="store_true")
    v.add_argument("--decode", action="store_true")
    v.add_argument("--max-tile-mib", type=positive, default=4)
    v.set_defaults(func=verify)
    return p


def main(argv=None):
    """Validate CLI limits, handle graceful termination, and return an exit code."""
    if sys.version_info < (3, 11):
        print("Python >= 3.11 is required", file=sys.stderr)
        return 2
    if sqlite3.sqlite_version_info < (3, 35, 0):
        print("SQLite >= 3.35 is required", file=sys.stderr)
        return 2
    p = parser()
    args = p.parse_args(argv)
    if args.command == "merge":
        args.max_inflight = args.max_inflight or args.workers * 2
        if args.tile_size > 4096:
            p.error("--tile-size must be <= 4096 (use small tiles for bounded decoder RAM)")
        if args.batch_mib < 3 * args.max_tile_mib:
            p.error("--batch-mib must be >= 3 * --max-tile-mib")
        if any(len(v.encode("utf-8")) > 65536 for v in (args.name, args.description) if v):
            p.error("metadata strings must be <= 64 KiB")
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        args.func(args)
        return 0
    except KeyboardInterrupt:
        print("Interrupted; rerun the same merge command to resume.", file=sys.stderr)
        return 130
    except (MergeError, sqlite3.Error, OSError, ValueError, ImportError, BrokenProcessPool) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
