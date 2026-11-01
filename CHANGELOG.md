# Changelog

**English** | [Čeština](CHANGELOG.cs.md)

## 2.0.0 — unreleased

### Breaking changes

- New outputs default to `pyramid`: import a common highest source zoom, then generate lower levels from the merged detail down to `--min-zoom` (default 0).
- Automatic selection requires equal actual highest zooms in all nonempty sources. An explicit `--base-zoom` must exist in every nonempty source. Other supplied levels are ignored in this mode.
- Managed schema 3 records the immutable mode/grid/resampling/lossless contract and durable overview work. Version 1.x outputs are refused without modification; rebuild from original inputs to a new output.
- `verify` returns an error for incomplete source imports or pending/unfinalized pyramid generation.
- `merge` no longer stores fully transparent source tiles (they used to be stored as unique tiles). `--keep-empty` restores that.

### Added and corrected

- `--mode overlay` retains the previous supplied-zoom compositing workflow for new outputs.
- Persisted, deduplicated dirty parents; bottom-up regeneration from four current children with correct TMS quadrant placement and premultiplied-alpha BOX averaging.
- Atomic parent images, dirty-key removal, ancestor scheduling and progress, with resume after failure or interruption in either phase.
- Incremental updates regenerate only affected ancestors; parents never accumulate old overview imagery.
- All output encoding is lossless. Unique lossy WebP is transcoded instead of copied, and `verify --decode` detects lossy WebP externally inserted into a managed archive.
- Shared worker backpressure, length-before-blob checks, bounded parent SQL batches and disk-backed scheduling. Both phases keep a single SQLite writer.
- Global phase, pending parents, cumulative overview writes and immutable settings in `status`; mode/phase metadata and actual generated output zoom extrema.
- Tests for neighboring contributions merged before downsampling, alpha edges, TMS, common-level errors, selective rebuilding and real SIGKILL before/after base and overview commits.
- Reproducible many-file/512px benchmarks, illustrated English/Czech documentation, and installed-wheel tests covering a complete pyramid.
- Publishing instructions explicitly distinguish repository enable variables from environment variables.
- `merge --reimport changed|always` overlays listed, already imported sources again over the current output (a new version of the same file, or an unchanged one) and regenerates affected ancestors; crash recovery never applies a committed batch twice. For sources with 0/255 alpha whose new version covers the old one the result equals a clean rebuild, and repeating it changes nothing.
- `merge --verify-recorded stat` checks recorded sources by stat fingerprint and hashes only those whose stat changed, for pipelines that append one source at a time to a large history.
- New `mbmerge tile` (needs GDAL ≥ 3.11 command-line tools): GeoTIFF/PDF/VRT/JPEG → flat lossless MBTiles for `merge`. PDF rasterization, collars (`--collar`, like `nearblack`) and exact colors (`--nodata`) turned into alpha before resampling, warp into the grid of the chosen zoom and polygon clipping **in that target grid**, so neighbours clipped by a shared boundary neither overlap nor leave cracks; `--binary-alpha`, empty-tile skipping, optional overviews for previews. The warp is read once into a temporary GeoTIFF in parallel stripes (`--threads`), so sources stored one row per strip are not decompressed again for every tile; `--webp-method` applies to GDAL-encoded tiles too.
- Minimum Pillow is 10.4 (tests pass with 10.4 and 13).

### Unchanged limits

No reprojection inside `merge`, source removal, Windows support or standalone executable build. A reimport does not remove pixels only an older version of a source had. Without `--verify-recorded stat`, historical sources are re-hashed on every invocation; import the initial collection in one command. Synthetic tests do not substitute for validating a real 1 TB workload.

## 1.1.0

Supplied-zoom incremental PNG/WebP compositing, bounded parallel image workers, single SQLite writer, durable source checkpoints, source change checks, metadata handling, MIT licensing and Python packaging/release workflows.
