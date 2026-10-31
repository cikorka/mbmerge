#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded, resumable raster MBTiles merger. See README.md for the contract."""

# Incrementally composite flat PNG/WebP MBTiles with bounded working data.
#
# Execution path: main -> merge -> import_source -> read_batch/bounded_results.
# Only the parent process owns SQLite connections. Spawned workers receive one
# tile job at a time and return encoded bytes; they never access a database.
#
# Three invariants are essential when changing this module:
# 1. Finish one source before starting the next: alpha compositing is ordered.
# 2. Commit pixels, metadata and the last source key together: repeating an
#    already committed translucent overlay changes its appearance.
# 3. Bound both encoded bytes and submitted jobs: a row limit alone does not
#    constrain image buffers, encoder results or executor queues.
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
VERSION = "1.1.0"
SCHEMA_VERSION = 2
MIB = 1024 * 1024
MERCATOR_LAT = 85.0511287798066
KEY = "zoom_level,tile_column,tile_row"
UPSERT = """INSERT INTO tiles VALUES (?,?,?,?)
 ON CONFLICT(zoom_level,tile_column,tile_row)
 DO UPDATE SET tile_data=excluded.tile_data"""


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
        if token(path) != expected:
            raise SourceChanged(f"Source changed while reading: {path}")
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
                if image_format(data) == output_format:
                    return z, x, y, data  # validate; preserve bytes in the selected format
                return z, x, y, encode(overlay, output_format, method, png_compress_level, max_blob)
            with decode(old, tile_size, max_blob, output_format) as base:
                with Image.alpha_composite(base, overlay) as result:
                    return z, x, y, encode(result, output_format, method, png_compress_level, max_blob)
    except Exception as e:
        raise MergeError(f"Tile {z}/{x}/{y}: {e}") from e


def worker_init():
    """Keep graceful signal handling and SQLite rollback in the parent."""
    # Parent handles graceful Ctrl-C/SIGTERM and transaction rollback.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def bounded_results(pool, jobs, args, metrics, source_format=None):
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
            pending.add(pool.submit(process_tile, job, args.tile_size,
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


def open_output(path, args):
    """Create/open a managed archive and resolve immutable encoding settings.

    Never adopt arbitrary pre-existing tiles without an import ledger. Legacy
    WebP settings are normalized in memory; migration becomes durable only in
    update_metadata's transaction with its corresponding pixels/checkpoint.
    This function resolves args.output_format for the worker pipeline.
    """
    db = sqlite3.connect(path, isolation_level=None, timeout=args.busy_timeout)
    try:
        objects = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
        if objects and "_merge_config" not in objects:
            raise MergeError("Output is an unmanaged database. Use a NEW output and pass the old MBTiles as the first --input.")
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
            """)
            settings = {"tile_size": args.tile_size, "webp_method": args.webp_method,
                        "output_format": args.output_format or "webp",
                        "png_compress_level": args.png_compress_level,
                        "name": args.name or path.stem,
                        "description": args.description or "Incrementally composited raster tiles",
                        "type": args.type or "overlay"}
            db.execute("INSERT INTO _merge_config VALUES(1,?,?)",
                       (SCHEMA_VERSION, json.dumps(settings)))
            _commit(db)
        row = db.execute("SELECT version,settings FROM _merge_config WHERE id=1").fetchone()
        if not row or row[0] not in (1, SCHEMA_VERSION):
            raise MergeError("Unsupported output schema version")
        settings = json.loads(row[1])
        # v1 outputs were always WebP. The next successful batch/final metadata
        # commit upgrades the ledger to v2, which old CLI versions refuse to open.
        if row[0] == 1:
            if settings.get("output_format", "webp") != "webp":
                raise MergeError("Invalid legacy output format")
            settings.setdefault("output_format", "webp")
            settings.setdefault("png_compress_level", 6)
        if settings.get("output_format") not in ("webp", "png"):
            raise MergeError("Invalid output format in configuration")
        if args.output_format is not None and args.output_format != settings["output_format"]:
            raise MergeError(f"Resume requires the original --output-format={settings['output_format']}; choose a NEW output to convert formats")
        args.output_format = settings["output_format"]
        frozen = ("tile_size", "webp_method" if args.output_format == "webp" else "png_compress_level")
        for k in frozen:
            if settings[k] != getattr(args, k):
                raise MergeError(f"Resume requires the original --{k.replace('_', '-')}={settings[k]}")
        for k in ("name", "description", "type"):
            if getattr(args, k) is not None:
                settings[k] = getattr(args, k)
        db.execute("CREATE TEMP TABLE batch_keys(z INTEGER,x INTEGER,y INTEGER,PRIMARY KEY(z,x,y)) WITHOUT ROWID")
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
    z0, z1, w, s, e, n = db.execute("""SELECT min(minzoom),max(maxzoom),min(west),min(south),
        max(east),max(north) FROM _merge_sources WHERE tile_count>0""").fetchone()
    meta = {"name": settings["name"], "description": settings["description"],
            "type": settings["type"], "version": "1.3", "format": settings["output_format"], "scheme": "tms"}
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
    where = "" if last is None else f"WHERE ({KEY}) > (?,?,?)"
    params = () if last is None else tuple(last)
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
        # Reserve the full output limit for overlaps and possible transcoding.
        # A declared matching source format is verified by the worker, so unique
        # matching tiles are byte copies and need only their original length.
        output_size = size if not old_size and source_format == args.output_format else max_blob
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
    metrics = {"batches": 0, "peak_inflight": 0, "peak_batch_bytes": 0}
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
            log(args, "plan", new_or_active=len(todo), skipped=sum(prior.get(str(p)) == "complete" for p in paths), workers=args.workers)
            if not paths and not active and not prior:
                raise MergeError("No inputs found")
            if todo:
                with ProcessPoolExecutor(max_workers=args.workers,
                        mp_context=multiprocessing.get_context("spawn"), initializer=worker_init) as pool:
                    for path in todo:
                        import_source(db, pool, path, args, settings, metrics)
            db.execute("BEGIN IMMEDIATE")
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
        checked = 0
        if args.decode:
            settings = json.loads(db.execute("SELECT settings FROM _merge_config WHERE id=1").fetchone()[0])
            for z, x, y, size in db.execute(f"SELECT {KEY},length(tile_data) FROM tiles"):
                if not valid_key(z, x, y) or not 0 < size <= args.max_tile_mib * MIB:
                    raise MergeError(f"Invalid output tile {z}/{x}/{y}")
            for z, x, y, data in db.execute(f"SELECT {KEY},tile_data FROM tiles"):
                with decode(data, settings["tile_size"], args.max_tile_mib * MIB,
                            settings.get("output_format", "webp")):
                    checked += 1
        unfinished = db.execute("SELECT count(*) FROM _merge_sources WHERE status!='complete'").fetchone()[0]
        print(json.dumps({"sqlite": "ok", "decoded_tiles": checked, "unfinished_sources": unfinished,
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
    m = sub.add_parser("merge", help="append inputs or resume; later inputs overlay earlier ones")
    m.add_argument("--output", required=True)
    m.add_argument("--output-format", choices=("webp", "png"),
                   help="new output: webp by default; existing output: use its recorded format")
    m.add_argument("--input", action="append", default=[], help="repeatable; order is compositing priority")
    m.add_argument("--input-dir")
    m.add_argument("--pattern", default="*.mbtiles", help="glob within input-dir; ** supported")
    m.add_argument("--input-list", help="UTF-8 paths, one per line; appended after --input")
    m.add_argument("--workers", type=positive, default=min(4, os.cpu_count() or 1))
    m.add_argument("--max-inflight", type=positive, help="default: 2 * workers")
    m.add_argument("--batch-size", type=positive, default=128, help="max source keys per transaction")
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
