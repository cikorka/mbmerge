# Verification report — 2026-11-01

**English** | [Čeština](TEST_REPORT.cs.md)

This report covers **mbmerge 2.0.0 / schema 3**, including base-level merge followed by bottom-up overview generation. Local host: macOS ARM64, Python 3.12.14, SQLite 3.53.1, Pillow 12.3.0, libwebp 1.6.0. Installed-distribution smoke checks also use Pillow 13.0.0. All inputs are synthetic. A real 1 TB collection has not been tested.

## Automated correctness and recovery

The complete local suite passed **30 tests**. Its console output/duration is in [TEST_RESULTS.txt](TEST_RESULTS.txt). The suite includes **16 actual process-group SIGKILL scenarios**: four in overlay source transactions, plus twelve pyramid cases covering source batches, intermediate overview batches and the last overview commit, before and after commit, for PNG and WebP.

Verified contracts:

- Default pyramid mode imports the actual common highest zoom, ignores supplied lower-zoom imagery, and derives parents only after the base imports finish. Asymmetric north/south/west/east colors check TMS orientation. Missing children remain transparent. Bounds stay tied to source/base extents rather than expanding to overview footprints.
- Alternating opaque red/blue contributions from separate sources become opaque purple after merging detail and then downsampling. This catches the incorrect pipeline of separately downsampling and overlaying source fragments. Invisible green RGB does not bleed into a red alpha edge.
- Unique lossy WebP is transcoded to VP8L lossless pixels in both modes; the original decoded pixels are preserved. Mixed PNG/WebP yields uniform lossless output. `verify --decode` rejects an externally injected lossy tile or incorrect format.
- Mismatched actual source maxima, missing explicit base levels and invalid minimum/base ranges fail safely. A rejected pristine plan can be corrected without deleting its empty output. Imported mode/base/minimum/format settings cannot silently change. Old schema 1/2 outputs are rejected byte-for-byte unchanged, including a committed uncheckpointed WAL: ownership/schema are probed read-only before opening for writes.
- A later contribution rebuilds only its affected ancestors. Unaffected branches retain identical blobs; the complete incremental output matches a fresh ordered rebuild. Completed source replay generates no new parents or pixel changes.
- A 96-source integration case supplies a common z=1 key in every file but only imports distinct z=8 base tiles. Parent generation equals the independent unique ancestor set; byte budgets and pending jobs remain within their configured limits.
- Actual SIGKILL tests leave SQLite integrity intact, committed source cursors exact, parent scheduling durable and unfinished outputs rejected by verify. Resume completes an omitted active source before a new contribution. Recovered tile blobs match uninterrupted builds, including after the last parent commits but global phase is still building.
- Existing safety tests retain priority/alpha/metadata behavior, input hashing with restored size/mtime, corrupt-image and pre-decoder dimension checks, PNG structure/modes, SQL rollback, checkpoint blocking, writer exclusion, source taint, source ordering and indexed query plans. A late-page 10,000-key source test measures actual SQLite VM work to guard against a zoom-only seek that rescans earlier base tiles.

Source-mutation fault injection tests the ledger response; it does not simulate every arbitrary concurrent raw-file edit. SIGKILL is real. Physical power failure, broken storage synchronization and all hardware/OS limits were not tested.

## Reimport, empty tiles and `tile` (real data)

`tests/test_reimport.py` and `tests/test_tile.py` add 16 tests; the full suite (46) passes with Python 3.11.2, SQLite 3.40.1, Pillow 10.4.0 and GDAL 3.11.4 ([TEST_RESULTS.txt](TEST_RESULTS.txt)) and with Python 3.13 / Pillow 13.0.0 without GDAL (9 GDAL tests skipped). They cover a changed source refused by default and overlaid by `--reimport changed` exactly like a fresh build, repeated `--reimport always` leaving binary-alpha outputs byte-identical, the documented partial-alpha accumulation, listed priority, stat verification that still catches an edit with restored mtime, skipped empty tiles, SIGKILL during a reimport of a translucent source resuming without a second overlay, and `tile` collars, nodata colors, cutlines, binary alpha, input order, PDF input and merge acceptance.

Real municipal land-use plans (115 documents, Benešov district, base zoom 17, 512 px):

- `tile` with clipping in the target grid: overlap between neighbouring municipalities 0 px (one-step clipping during reprojection: 9,917 px for five neighbours) and no cracks along shared boundaries (18,435 → 0; the remaining 105 lie inside individual documents).
- `tile` reads the warp once, in parallel stripes, instead of tiling through warped VRTs: the largest plan (31,000 × 28,000 px, stored one row per strip) 126 → 27 s; twelve documents 378 → 165 s, or 115 s with four documents at once. Against an exact warp (`-et 0`) the materialized pixels stay within 1; `gdalwarp` writing straight to a file was faster but off by up to 82 in 8 % of pixels, so it is not used.
- `--reimport always` of all five documents, twice: output byte-identical to the first merge (3,457 tiles).
- `--verify-recorded stat` over 115 recorded sources: 0.38 s.
- Reimport of three documents into a 49,496-tile WebP output while MapProxy served the same file: 12,100 requests, 0 failures, no blocked checkpoint.

## Many-file, 512px memory and fan-in measurements

Each file contains one distinct PNG z=14 tile and a supplied z=1 tile shared by every file. The default pipeline must ignore the supplied z=1 imagery, import only the base and generate each required ancestor once. Four workers, max-inflight 8, 32 MiB encoded batch budget, 4 MiB tile cap and default compression settings. Every run starts with a new output.

| Output | Sources | Base tiles | Parents generated | Time (s) | Tree RSS (MiB) | In-flight peak | Batch bytes peak |
|---|---:|---:|---:|---:|---:|---:|---:|
| webp | 128 | 128 | 134 | 9.473 | 269.3 | 7 | 29361724 |
| webp | 7000 | 7000 | 2382 | 554.466 | 262.81 | 7 | 29363178 |
| png | 128 | 128 | 134 | 20.771 | 217.91 | 7 | 29391138 |
| png | 7000 | 7000 | 2382 | 451.234 | 267.59 | 7 | 29422904 |

All four runs passed full SQLite integrity checks and decoding of every output tile. Independently computed ancestor coordinates matched the entire output key set. Contributions equaled the source count, pending parents were zero and phase was complete. Both 7000-file runs generated **one z=1 tile**, rather than compositing 7000 supplied low-zoom images. The reported count of generated overviews matched the unique expected ancestor set.

Raw input/output sizes, runtime versions, process counts, metrics and validation results are in [BENCHMARK.json](BENCHMARK.json). The fixtures use simple repeated imagery and small per-file data, not 1 TB of complex maps. They exercise many-file planning, source ledgers, commits, durable dirty work, actual 512px codecs and shared low-zoom fan-in. Do not extrapolate TB throughput from these timings.

RSS is sampled about every 100 ms with ps, summing the CLI parent, its workers and resource tracker. The benchmark driver, fixture preparation, output verification and OS filesystem cache are excluded. Shared pages may be counted multiple times and brief peaks may be missed. The two encoders and some development checks overlapped on the host; times are observations under that load, not an isolated speed comparison. Process-tree measurements track each CLI separately. These are not hard RSS limits.

An early workspace-based attempt stopped when an input fingerprint changed before the run completed. It is not counted as a successful benchmark. Fixture generators now explicitly close SQLite connections (a connection context manager only commits), and the final runs use a temporary directory. The initial event's exact cause was not established; source integrity checks were not relaxed or bypassed. The CLI now identifies which stat fields changed when reporting this error.

## Distribution and documentation checks

Wheel and sdist passed strict Twine metadata validation and `check_release.py` identity/content/tag checks for v2.0.0. A wrong version tag is refused. Installation of the wheel and a wheel rebuilt from sdist passed the isolated installed-CLI test outside the checkout, using real workers, mixed 512px sources, both lossless encoders, a complete pyramid, exact base pixels, TMS checks, metadata, replay and full output decoding. English/Czech links and illustrated README assets were checked locally.

GitHub-hosted CI results are available through the branch/PR Actions checks. The recorded local results above do not claim every supported runner was locally available. No release tag, GitHub Release, PyPI or TestPyPI upload is performed by these validation commands.

## Remaining operating limits

Historical sources must remain immutable and available at their recorded paths. Every invocation re-hashes their contents unless `--verify-recorded stat` is used; use one invocation for the large initial list. Replacing/removing layers, Windows and standalone executables remain unsupported; GDAL inputs, reprojection and polygon clipping belong to `mbmerge tile`, not to `merge`. Eight-bit compositing and resampling round values; lossless encoding does not mean lossless downscaling. Publish only complete closed archives, avoid long WAL readers, use local SQLite-compatible storage and enforce any absolute memory cap at the OS level. Pilot representative real data before production.
