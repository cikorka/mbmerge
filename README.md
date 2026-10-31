# mbmerge

**English** | [Čeština](README.cs.md)

Incrementally merge raster PNG and WebP MBTiles archives with alpha compositing, bounded batches, parallel image processing, and a single SQLite writer. Sources are imported in order; later layers are composited over earlier ones. Work resumes from the last committed batch after interruption.

**Supported:** Linux/macOS (POSIX), Python 3.11+, SQLite 3.35+, and Pillow (WebP support is required when reading or writing WebP). Inputs must contain flat, indexed TMS tiles of the same square pixel size. Static PNG and WebP inputs can be mixed. JPEG, vector tiles, and normalized MBTiles layouts are not supported.

## What gets merged?

The output contains the union of tile coordinates from all inputs. When two sources contribute neighboring or overlapping image fragments **at the same `z/x/y`**, their pixels are composited into one output tile. Tiles at different coordinates remain separate tiles in the combined archive.

![Two source archives contribute to a shared tile coordinate; the output keeps their unique tiles and composites the shared tile.](docs/images/tile-grid.png)

The example below uses the actual bundled 512 × 512 px WebP inputs. A checkerboard represents transparency. The later source is drawn over the earlier source, including partially transparent pixels in the overlap.

![Actual demo tiles: source A, source B, and their alpha-composited result at the same tile coordinate.](docs/images/compositing.png)

## Installation

The package, repository and command are named **mbmerge**. Once the first release is published to PyPI:

```bash
pipx install mbmerge
mbmerge --version
```

Before that, or when working from source, use the checkout installation below. `python -m mbmerge` is supported too; the original `python merge_mbtiles.py` invocation remains compatible.

## Install from a checkout and try the demo

From the repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v

mbmerge merge \
  --input demo/source_a.mbtiles --input demo/source_b.mbtiles \
  --output demo/merged.mbtiles --workers 2 --name "Raster overlay demo"
mbmerge verify --output demo/merged.mbtiles --decode
mbmerge status --output demo/merged.mbtiles
```

The repository includes `demo/source_a.mbtiles`, `demo/source_b.mbtiles`, their PNG previews, `demo/expected_overlap.png`, and equivalent PNG MBTiles inputs in `demo/png/`. Coordinates are synthetic. The command creates your own `demo/merged.mbtiles`. A prebuilt managed output is not published because its import ledger would contain local absolute paths.

Generate another demo set, or regenerate the README images:

```bash
python examples/create_demo.py --output-dir new_demo
# Or generate PNG MBTiles:
python examples/create_demo.py --output-dir new_demo_png --tile-format png
python examples/create_readme_images.py --demo-dir demo --output-dir docs/images
```

## PNG and WebP

Use `--output-format png` for a PNG archive or `--output-format webp` for WebP. New outputs default to **WebP**, even when all sources are PNG. On resume, omitting the option uses the existing output's recorded format. PNG and WebP sources may be mixed; every output tile uses the selected format, and `metadata.format` reflects it.

```bash
# Preserve PNG as PNG, including transparent overlaps:
mbmerge merge \
  --input-dir demo/png --output demo/png-merged.mbtiles \
  --output-format png --png-compress-level 3

# Convert PNG sources into lossless WebP tiles:
mbmerge merge \
  --input-dir demo/png --output demo/webp-from-png.mbtiles \
  --output-format webp
```

PNG compression effort is 0–9 (default 6); a lower level is generally faster and larger, without changing decoded pixels. It applies to newly encoded tiles. Unique tiles already in the selected format keep their original bytes. Other unique tiles are transcoded; overlaps are always composited and encoded in the output format. Encoding does not restore losses already present in a source.

Static PNG up to 8 bits per channel is supported, including indexed palettes/transparency, grayscale, grayscale+alpha, RGB, and RGBA. Animated PNG/WebP and 16-bit PNG are rejected; convert 16-bit inputs to an explicit 8-bit display representation first. Declared source formats must match tile contents; when `metadata.format` is absent, tile signatures are detected automatically.

The output format is fixed for the life of an archive. To change it, rebuild into a new output from the original sources. The active encoder setting (`--webp-method` or `--png-compress-level`) must also match on resume. Outputs from version 1.0 remain WebP; version 1.1 upgrades their ledger from schema 1 to 2 on the next successful batch or final metadata commit. Use version 1.1+ afterward: version 1.0 intentionally refuses schema 2.

## Does an existing output get overwritten?

**An output created by this CLI is extended, not reset.** Re-running the same command validates and skips completed inputs. New inputs are appended, and tiles they overlap are composited with the existing result. If an already imported input has changed, the run fails safely before adding new tiles. `--resume` is not needed.

An existing MBTiles file created by another tool has no import ledger for this CLI. Passing it as `--output` is rejected without adopting or replacing its data. To merge it, use it as the first input and choose a new output:

```bash
mbmerge merge \
  --input existing.mbtiles --input additional.mbtiles \
  --output merged.mbtiles

# Subsequent runs can extend the managed output:
mbmerge merge --input next.mbtiles --output merged.mbtiles
```

This preserves the original archives and establishes the history needed for safe resume. It requires space for the new output. The output cannot also be an input, have hard links, or match the input directory's glob.

## Start with a representative subset

Use a small set of immutable sources with representative overlaps. Keep the output outside the input directory:

```bash
mbmerge merge \
  --input-dir /data/pilot/inputs --output /data/pilot/merged.mbtiles \
  --workers 4 --max-inflight 8 --batch-size 128 --batch-mib 32 \
  --cache-mib 32 --source-cache-mib 8 \
  --name "Raster mosaic" --type overlay \
  --json-log 2> /data/pilot/merge.jsonl
```

Resume after Ctrl-C, SIGTERM, SIGKILL, or restart by running the same command. Exit codes: 0 success, 1 operational error, 2 invalid arguments, 130 handled interruption. On failure, the current uncommitted batch is rolled back.

You can extend the pilot output with the full set if the original absolute input paths remain unchanged. Copies at new paths are new layers; use a new output for an independent rebuild. Check overlap edges, total process memory, throughput, and free disk space. Synthetic benchmark throughput does not predict runtime for a different dataset. A real 1 TB dataset has not been tested; see the [verification report](TEST_REPORT.md).

## Source order and incremental imports

1. Repeated `--input` arguments, in argument order.
2. Entries in `--input-list`, in line order.
3. Files from `--input-dir`, sorted lexicographically by absolute path. The default pattern is `*.mbtiles`; use `--pattern '**/*.mbtiles'` to include subdirectories.

A later source is composited **over** the existing image using `Image.alpha_composite`. Partially transparent pixels blend; fully opaque pixels in the later source take precedence. Historical priority is recorded in the output. New files are always appended, even if their names sort before old ones. An unfinished source is completed before any new source, even when omitted from the next command.

Repeated occurrences of the same path in one invocation are deduplicated. Different hard links to the same input are rejected. Identical bytes in separate physical files are not automatically deduplicated.

A UTF-8 `--input-list` has one path per line. Blank lines and lines starting with `#` are ignored. Relative paths resolve against the list's directory. Interior spaces are preserved; filenames with newlines or leading/trailing whitespace are unsupported.

```bash
mbmerge merge \
  --input-list /data/inputs.txt --output /data/merged.mbtiles --workers 4

# Append a new source later:
mbmerge merge \
  --input /data/new/source_next.mbtiles --output /data/merged.mbtiles
```

## Memory and backpressure

Workers use `spawn` and receive only bounded blobs and image parameters. They receive no SQLite connections and do not inherit open SQLite connections from the parent.

- Source reads use an indexed keyset condition `(z,x,y) > last_key`, without `OFFSET`. Keys and blob lengths are read first.
- Overlaps are located with a batch `JOIN` against a small temporary table. Join order is fixed to avoid scanning the entire output for each batch.
- `--batch-mib` covers source blobs, existing blobs, and output capacity: the full `--max-tile-mib` for overlaps or possible transcoding. Unique tiles declared in the selected format reserve their original length, since the worker verifies that declaration and copies their bytes. Small compressed inputs therefore reserve space for a potentially larger lossless composite.
- At most `--max-inflight` jobs are submitted, including completed results not yet consumed. More are submitted only after results are consumed. There is no unbounded `executor.map` queue.
- Results are written with bounded `executemany` buffers. Each source batch, its metadata, and its resume checkpoint commit in one transaction.
- Blob lengths and image dimensions are bounded. PNG IHDR and VP8X/VP8/VP8L dimensions are checked before decoding; PNG chunk CRCs and static structure are validated too. Animated images are rejected. SQLite mmap is disabled, caches are explicitly limited, and temporary tables may spill to disk.

| Option | Default | Purpose |
|---|---:|---|
| `--output-format` | WebP for new outputs | `png` or `webp`; inferred from existing output on resume |
| `--png-compress-level` | 6 | PNG compression effort 0–9, without changing pixel quality |
| `--workers` | up to 4, based on CPU | image validation and compositing processes |
| `--max-inflight` | 2 × workers | maximum submitted jobs |
| `--batch-size` | 128 | maximum source keys per transaction |
| `--batch-mib` | 32 | encoded-data budget including output capacity |
| `--max-tile-mib` | 4 | maximum source, existing, or output tile blob |
| `--sql-batch-size` | 16 | maximum rows per executemany |
| `--sql-batch-mib` | 4 | SQL buffer budget; a larger permitted blob is written alone |
| `--cache-mib` | 32 | output SQLite cache |
| `--source-cache-mib` | 8 | current source SQLite cache |
| `--tile-size` | 512 | square tile size, at most 4096 px |
| `--webp-method` | 4 | compression effort 0–6, always lossless |
| `--busy-timeout` | 5 s | SQLite lock/checkpoint wait |
| `--log-interval` | 5 s | progress interval; source completion is always logged |

`--batch-mib` must be at least three times `--max-tile-mib` so a maximum-size pair and its output fit. A smaller-memory configuration for 512 px tiles:

```bash
mbmerge merge \
  --input-dir /data/inputs --output /data/merged.mbtiles \
  --workers 2 --max-inflight 2 --batch-size 64 \
  --max-tile-mib 2 --batch-mib 12 --cache-mib 16 --source-cache-mib 4
```

The batch budget **is not a hard limit on total RSS**. Python/Pillow processes, serialization, blob copies, decoded images, and libwebp working memory add overhead. Memory grows roughly with `batch_mib + max_inflight × max_tile_mib + workers × tile_size²`, caches, and process overhead, rather than with total tile count. Use an OS limit such as Linux cgroup/systemd `MemoryMax` when an absolute cap is required. After an OOM kill, resume from the committed batch. The list of input paths is held in memory; the contents of all tiles are not.

Every tile is decoded for validation. Non-overlapping tiles already in the selected format retain their **original encoded bytes**, without recompression. Other tiles are encoded as lossless PNG or WebP (`lossless=True, exact=True`). This cannot undo lossy compression already present in an input. Each additional source may decode and encode the same low-zoom tile again; this implementation prioritizes incremental importing over a two-phase grouping of all sources.

## Transactions, crashes, WAL, and storage

The output contains standard `tiles` and `metadata` tables plus `_merge_*` tables. The ledger stores absolute source paths, SHA-256 hashes, informational stat fingerprints, source order, status, last committed key, counts, and extents. Configuration stores the schema version and output settings.

Tile changes, metadata, and the last key commit atomically. A crash before commit rolls back the batch; a crash after commit skips it on resume. This matters because repeating alpha compositing is not idempotent. An `active` source becomes `complete` only after the final SHA-256 check.

The output uses WAL, `synchronous=FULL`, and `fullfsync`/`checkpoint_fullfsync` where supported. Source registration, each batch, and completion are followed by `wal_checkpoint(TRUNCATE)`. If an external reader blocks it, importing **stops** so WAL cannot accumulate across further batches. Close the reader and resume. `journal_size_limit=0` alone is not a hard WAL cap; WAL includes SQLite pages and indexes as well as blobs.

Avoid long output-reader transactions during import. The cooperative `*.merge.lock` lock excludes other CLI instances. The file remains after completion and **must not be deleted while a merger is running**. Do not edit output tiles or internal tables outside the CLI.

Use local storage with SQLite WAL support; network shares/NFS are unsupported. Allow room for the output, a batch-sized WAL with page/index overhead, and a free-space margin. No automatic `VACUUM` or full working copy of the output is created. Publish or back up a closed output after a successful checkpoint; do not copy only the main MBTiles file while an uncheckpointed WAL exists. Power-loss durability also depends on correct disk/filesystem synchronization. During import, readers can see only the committed part of an unfinished source.

## Changed sources and safe recovery

Inputs must be **offline, closed, and immutable**. They need a flat `tiles` table and a unique ascending BINARY index on `(zoom_level,tile_column,tile_row)` in that order. Normalized `map/images` layouts, views, and WAL-mode inputs are rejected. Export these to flat MBTiles first. For a closed WAL input, checkpoint it offline and switch to `journal_mode=DELETE`; **never delete a WAL containing data by hand**.

Before each run, SHA-256 is recalculated for **every recorded source**, including sources omitted from the new command. Each new/active source is also hashed before reading and at completion. This costs extra sequential I/O but detects changes with the original size and mtime restored. A stat-only verification mode is intentionally unavailable. Hashing a large source can take time before the first tile-progress message.

A SQLite read transaction remains open during hashing and import, blocking normal concurrent SQLite writes. Batch boundaries also check device, inode, size, `mtime_ns`, `ctime_ns`, and journal/WAL sidecars. A source change detected during importing marks the output `tainted`, requiring a new output. Deliberately bypassing locks or making arbitrary concurrent raw-file writes is outside the contract; use immutable copies or snapshots.

A different SHA-256 for a recorded source stops preflight **before new tiles are added**. Restore the exact original bytes at the recorded path, or rebuild into a new output. Overlaying a changed source would leave pixels removed from the new version. This release therefore does not replace/remove earlier layers, relocate recorded paths, or offer `--force-changed`. Keep source files available after import. A preflight hash mismatch alone does not taint the output, so restoring the original bytes permits resume.

## TMS, image formats, and metadata

Inputs use Web Mercator/TMS: row 0 is south. Zooms 0–30 are supported, with integer `x,y` in `[0,2^z)`. The CLI flips neither coordinates nor image pixels. Missing `scheme` means TMS is assumed; explicit XYZ is rejected. Incorrect CRS or TMS labeling cannot be detected automatically. Static PNG and WebP tiles of the same dimensions are accepted as described above; RGB is fully opaque. JPEG, animations, 16-bit PNG, and PBF are rejected.

Metadata updates atomically with each batch and at completion:

- `minzoom/maxzoom`: derived from committed tile keys, ignoring declared source zooms. The small source ledger is aggregated instead of repeatedly scanning the tiles table.
- `bounds`: union of valid source bounds, falling back to the full extents of imported TMS tiles when bounds are absent/invalid. Numerically valid source bounds are trusted. After its first batch, an unfinished source with bounds contributes its full declared extent.
- Antimeridian crossings conservatively expand to `[-180,180]`. Alpha-channel edge cropping is not calculated. Fully transparent stored tiles still contribute to zooms and fallback extents.
- `center`: geographic bounds midpoint, zoom = minzoom; not an optimized viewport.
- `format=png` or `webp` according to the output selection, `scheme=tms`, `version=1.3`, `type=overlay` or `baselayer`.
- `name`, `description`, and `type` may be updated on subsequent runs; omitted values are preserved. `--tile-size`, the output format, and its active encoder setting must match the original run.
- `attribution`: unique complete strings, sorted and joined with ` | `. Maximum 64 KiB per source value, 1 MiB for the merged attribution. Empty sources contribute no bounds, zooms, or attribution.

## Verification and reproducible measurements

```bash
# Small ledger and metadata only:
mbmerge status --output /data/merged.mbtiles

# quick_check scans the database and can take a long time:
mbmerge verify --output /data/merged.mbtiles

# Full index checks plus decoding every image, slower still:
mbmerge verify --output /data/merged.mbtiles \
  --full-sqlite-check --decode

# Simultaneous parent+descendant RSS sampling; requires ps.
# About 1.2 GiB of temporary disk space; databases are removed afterward:
python tests/benchmark_memory.py --tiles 64 512 --work-dir /data/tmp > benchmark.local.json
# Repeat for PNG output:
python tests/benchmark_memory.py --tiles 64 512 --output-format png --work-dir /data/tmp > benchmark-png.local.json
```

`verify` checks managed outputs. It does not compare their content with sources or verify source hashes. It reports the number of unfinished sources. Source hashes are checked by `merge`, even when no new files are supplied. Tests cover transparency and overlaps, metadata and TMS, priority, unchanged bytes for unique tiles, bounded jobs/batches, changes with preserved size/mtime, corrupt images, dimensions checked before decoding, rollback after partial SQL writes, blocked checkpoints, writer exclusion, format conversion and PNG validation, legacy-ledger migration, and actual SIGKILL before/after commit for both output formats.

See [TEST_REPORT.md](TEST_REPORT.md) for measured results and limitations. References: [SQLite WAL](https://www.sqlite.org/wal.html), [SQLite PRAGMA/synchronous](https://www.sqlite.org/pragma.html), [Pillow image formats](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html), [MBTiles 1.3](https://github.com/mapbox/mbtiles-spec/blob/master/1.3/spec.md).

## Releases and PyPI

[RELEASING.md](RELEASING.md) explains versioning, one-time PyPI/TestPyPI setup and the GitHub Actions workflows. Commits/PRs run tests and package checks; only a published stable GitHub Release can publish to PyPI. A manual workflow publishes to TestPyPI. Publishing also requires a public repository and its explicit enable variable; private evaluation runs checks only. The release tag must exactly match `v` plus the single `VERSION` in `merge_mbtiles.py`.

## Architecture and maintenance

See [ARCHITECTURE.md](ARCHITECTURE.md) for the code map, source state machine, transaction boundaries, memory accounting and extension guidance. Source comments and function docstrings are in English. The CLI and tests carry the `SPDX-License-Identifier: MIT` header.

## What belongs in Git?

Commit the Python source, tests, examples, documentation and images, the synthetic `demo/source_*.mbtiles` and `demo/png/source_*.mbtiles` fixtures, and curated verification reports. The commented [.gitignore](.gitignore) excludes other MBTiles/SQLite databases, generated outputs, local data directories, logs, virtual environments, caches, and local environment files. It does not remove files that are already tracked. Keep `.env.example` free of credentials if you add one.

## License

Released under the [MIT License](LICENSE.md). Copyright (c) 2026 mbmerge contributors. A [non-binding Czech translation](LICENSE.cs.md) is available; the English license is authoritative.
