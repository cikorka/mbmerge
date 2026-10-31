# Changelog

**English** | [Čeština](CHANGELOG.cs.md)

## 2.0.0 — unreleased

### Breaking changes

- New outputs default to `pyramid`: import a common highest source zoom, then generate lower levels from the merged detail down to `--min-zoom` (default 0).
- Automatic selection requires equal actual highest zooms in all nonempty sources. An explicit `--base-zoom` must exist in every nonempty source. Other supplied levels are ignored in this mode.
- Managed schema 3 records the immutable mode/grid/resampling/lossless contract and durable overview work. Version 1.x outputs are refused without modification; rebuild from original inputs to a new output.
- `verify` returns an error for incomplete source imports or pending/unfinalized pyramid generation.

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

### Unchanged limits

No direct GDAL input, reprojection, polygon clipping, source replacement/removal, Windows support or standalone executable build. Historical sources remain immutable and are re-hashed on every invocation. The initial collection should be imported in one command. Synthetic tests do not substitute for validating a real 1 TB workload.

## 1.1.0

Supplied-zoom incremental PNG/WebP compositing, bounded parallel image workers, single SQLite writer, durable source checkpoints, source change checks, metadata handling, MIT licensing and Python packaging/release workflows.
