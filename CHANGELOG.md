# Changelog

**English** | [Čeština](CHANGELOG.cs.md)

## 2.4.0 — 2026-11-07

- `tile --resume`: progress (`rows_done` in `tiling` and the final `base` event, which now carries `rows_done`/`rows`) counts the rows finished before the interruption, so it is the progress of the whole output, not of the current run.
- `mbmerge grade` measures one colour grade (levels per channel, gamma, saturation, vibrance) for a whole mosaic from all inputs read at 1/8 (99 orthophoto sheets: 13 s on 12 threads); `--preview` renders before/after crops. `tile --grade` applies it to every pixel before encoding — one function everywhere, so no seams between sheets — and records it in the metadata and the resume contract; `compare --grade` grades the reference too. Graded orthophoto at q85: tiles about 45 % larger.

## 2.3.1 — 2026-11-06

- `tile --cutline`: rows of tiles that do not touch the polygon are no longer read from the sources (they were decoded, warped and then clipped to transparency). A country boundary has its bounding box about half outside the country, so a national run spent hours on rows without a single tile. Output is byte-identical; an interrupted 2.3.0 run can be continued with `--resume`.

## 2.3.0 — 2026-11-06

- `tile --resume` continues `<output>.tmp` left by a failed or killed run (committed after every row of tiles, WAL). Finished work rectangles and overview levels are skipped; inputs (path, size, mtime) and settings must be unchanged, `--threads` may differ. Measured on an orthophoto killed after 10 minutes: the resumed output is byte-identical to an uninterrupted run.
- `tile --cutline` limits the warp to the intersection of the polygon with the input. A country boundary over one orthophoto sheet used to walk the empty rows of the whole country (estimated 27 h instead of 30 min).
- `tile --quality` builds every level below the workers' in-memory levels from lossless copies kept in a side file, never from decoded lossy tiles (z13 of an orthophoto: +0.7 dB PSNR).
- `tile --worker-cache-mib` (default 128, before a fixed 256): about 30 % less memory per worker at the same speed, so 32 workers fit in 16 GB. Orthophoto of 99 sheets to z19: 16 workers 1187 s, 32 workers 637 s.
- `tile` reads the headers of plain inputs in-process and gives `--s-srs` once to the whole mosaic when every input needs only that (no PDF, palette, `--collar`, `--nodata`): preparing 16 316 orthophoto sheets took 13 s instead of 38 min. Mosaics take the finest input resolution (`-resolution highest`), not the average. A JPEG with a broken EXIF block (reported as an error, pixels fine) no longer stops the header check or the workers; a read that returns no data still does.
- `mbmerge compare` scores MBTiles against a direct render of their sources (PSNR, 5th percentile, SSIM per zoom; needs scikit-image). `tools/benchmark_tile_workers.py` measures worker memory and speed in a fixed window.

## 2.2.0 — 2026-11-03

- `tile` builds the first four levels below the base (`PYRAMID_DEPTH`) in the worker processes, from the pixels they have just encoded, instead of decoding stored tiles: work rectangles are aligned to blocks of 16 × 16 tiles and one pending row per level is kept (about 30 MB per worker). Lossless outputs are pixel-identical to 2.1.0 at every zoom. With `--quality 85`, z18–z15 are about 1 dB closer to the lossless pyramid (no repeated lossy round trip), and an orthophoto of 12 sheets took about 9 % less CPU time. Small rasters with fewer rectangles than workers use a shallower depth, so they keep using every worker.
- Overview tile keys are stored as integers (a numpy integer would have been stored as a BLOB).

## 2.1.0 — 2026-11-02

### Changed

- `mbmerge tile` writes base tiles straight into the MBTiles. Worker processes (`--threads`) read the warp in rectangles row by row (full width for narrow rasters, 16-tile bands for wide mosaics), burn the cutline into alpha in the target grid and encode; the parent is the only SQLite writer. No temporary stripes or tile directories on disk. Base tiles of land-use plans are pixel-identical to 2.0.0; an orthophoto of 12 JPEG sheets to z19 took 536 s instead of 780 s.
- `mbmerge tile` now also needs the GDAL Python bindings (`osgeo`) of the same version as the GDAL command-line tools, and numpy.
- Pyramid levels in `merge` and `tile` use a premultiplied `reduce(2)` instead of a BOX resize, whose rounding biased each level by +0.5 (about 2–3 levels four zooms down, now 0.8–1.2). Lower zooms of outputs rebuilt with 2.1.0 therefore differ slightly from 2.0.0; base tiles do not change.

### Added

- `tile --quality Q`: lossy WebP for imagery that is published as is (orthophotos: 184 s and 1.0 GB instead of 536 s and 6.7 GB). Layers that are merged should stay lossless.
- `tile --s-srs SRS`: inputs without a coordinate system (world files such as `.jgw`) are refused unless it is given; GDAL used to read their S-JTSK coordinates silently as Web Mercator metres.
- `tile` expands palette inputs to RGBA and logs progress every 30 s.

## 2.0.0 — 2026-11-02

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
