# Verification report — 2026-10-31

**English** | [Čeština](TEST_REPORT.cs.md)

`merge_mbtiles.py` version 1.1.0 was tested on macOS ARM64, Python 3.12.14, SQLite 3.53.1, Pillow 12.3.0, and libwebp 1.6.0. These are synthetic tests. A real 1 TB input set and a complete import of thousands of source archives have not been tested. Validate a representative subset before production use.

## Automated tests

`python -m unittest discover -s tests -v`: **22 tests, OK**. The latest run output and duration are in [TEST_RESULTS.txt](TEST_RESULTS.txt).

Verified scenarios:

1. Partially transparent overlaps, later-source priority, transparent regions, exact pixel comparison with Pillow alpha_composite, unchanged bytes for unique tiles, unchanged TMS rows, bounds/center/zooms/attribution, incremental addition, and skipping completed inputs.
2. Fallback bounds for southern TMS rows, invalid NaN bounds, and conservative antimeridian handling.
3. Changed source content with the original length and mtime restored: SHA-256 rejects the run before a new layer is added, including a recorded source omitted from the new command. Restoring its exact original bytes permits resume.
4. Corrupt WebP, wrong image dimensions, invalid TMS keys, missing unique indexes, and explicit XYZ: errors without new committed pixels.
5. The byte budget limits batches even with small highly compressed inputs. `peak_inflight <= 2` and `peak_batch_bytes <= 3 MiB` under the test settings.
6. A worker failure after some SQL writes rolls back all pixels and checkpoint changes in the current batch.
7. A long-lived external WAL reader blocks checkpointing; importing stops after source registration and resumes after the reader closes, without repeating compositing.
8. Oversized blobs are rejected by length before their contents are fetched. Forged oversized WebP dimensions are rejected before decoding.
9. Output-as-input rejection, refusing to adopt an unmanaged output, and exclusion of concurrent CLI writers.
10. Rejection of WAL-mode inputs.
11. A simulated unexpected source change during import persists the `tainted` state and prevents future importing.
12. `EXPLAIN QUERY PLAN`: keyset reads use the index without temporary sorting; the output join performs indexed lookups.
13. Manifest ordering, directory sorting, and empty sources.
14. Actual SIGKILL of the process group for both PNG and WebP outputs, with a PNG source, before commit and after commit but before WAL checkpoint: SQLite integrity_check remains OK. The ledger contains 2 tiles before the targeted commit and 4 after it. Resume finishes the active source before a new layer. Both recovered outputs are **byte-identical** to the uninterrupted run. Re-importing a completed partially transparent source leaves the result unchanged.

The source-mutation test injects `SourceChanged`; it does not race arbitrary raw-file writes against SQLite. The SIGKILL scenarios actually terminate processes. Physical power loss and hardware fsync behavior were not tested.

## PNG/WebP coverage added in version 1.1

Eight additional tests cover:

1. PNG-to-PNG alpha compositing, original-byte preservation for a unique PNG tile, correct `metadata.format=png`, and full output decoding.
2. Mixed WebP/PNG inputs into each output format: exact RGBA pixels, uniform encoding for every output tile, unique-byte preservation where possible, and replay without changing results.
3. Signature detection without format metadata, plus indexed PNG transparency, grayscale, grayscale+alpha, and RGB.
4. PNG dimensions checked before `Image.open`, bad chunk CRCs, APNG rejection, explicit 16-bit PNG rejection, and mismatched format declarations.
5. Output format and active compression setting frozen on resume, with PNG inferred when the output-format option is omitted.
6. A legacy schema-1 WebP ledger upgraded to schema 2 while importing a new PNG source; attempting to change its output format is rejected.
7. Worst-case output reservation for unique tiles that need transcoding, bounded jobs/batch bytes, and encoded-output size enforcement.
8. Output verification rejecting a tile encoded differently from the selected output format.

The original published 1.0 CLI was also checked manually against a schema-2 PNG output and refused it as intended. Migration changes the ledger version, so use 1.1+ after a successful upgrade.

## Load and memory

Version 1.1.0 was measured with `python tests/benchmark_memory.py --tiles 64 512 --work-dir <temporary-directory>`, then again with `--output-format png`.

Both inputs contain 512 × 512 px lossless RGBA WebP with deterministic random color data and transparency, with all coordinates overlapping. The PNG runs therefore also exercise format conversion. Four workers, at most eight submitted jobs, a 32 MiB batch budget, a 4 MiB blob limit, and other defaults. Each run starts with a new output. Column labels identify output format and tiles per source.

| Metric | WEBP · 64 | WEBP · 512 | PNG · 64 | PNG · 512 |
|---|---:|---:|---:|---:|
| Tiles per source | 64 | 512 | 64 | 512 |
| Contributions from both sources | 128 | 1024 | 128 | 1024 |
| Combined input size (B) | 101228544 | 809558016 | 101228544 | 809558016 |
| Output size (B) | 51412992 | 405577728 | 58437632 | 460922880 |
| Total time including monitoring (s) | 4.14 | 33.246 | 5.087 | 30.231 |
| Peak process-tree RSS (MiB) | 423.67 | 390.14 | 313.25 | 321.27 |
| Concurrent processes | 6 | 6 | 6 | 6 |
| Maximum submitted jobs | 8 | 8 | 6 | 6 |
| Maximum batch budget used (B) | 33130860 | 33130860 | 29898804 | 29898804 |
| Committed data batches | 17 | 128 | 24 | 189 |

RSS is sampled approximately every 100 ms using `ps`, summing the parent, workers, and resource tracker. Shared pages may be counted more than once. This is a sampled practical measurement, not an exact sum of private allocations. It excludes OS filesystem cache, can miss brief peaks, and is not a hard RSS cap. The larger runs did not show growth proportional to the eightfold input increase; sampling and runtime variation can also make a smaller run's measured peak higher. Raw data and runtime versions are in [BENCHMARK.json](BENCHMARK.json). Throughput depends on data, overlaps, storage, cache, and compression; these results do not predict runtime for a 1 TB dataset.

## Demo and README images

The bundled PNG inputs in `demo/png/` were merged into temporary PNG and WebP outputs. `verify --full-sqlite-check --decode` succeeded: one decoded output tile, zero unfinished sources. Both outputs match `demo/png/expected_overlap.png` exactly in decoded RGBA pixels; re-running skips both recorded sources. The output is not published because its ledger contains local absolute paths; create your own using the [README](README.md).

The [compositing image](docs/images/compositing.png) is rendered from the actual demo previews and expected pixels. The [tile-grid diagram](docs/images/tile-grid.png) illustrates both unique coordinates and a shared coordinate: fragments sharing `z/x/y` produce one tile, while different coordinates stay distinct. README images can be regenerated with `examples/create_readme_images.py`.

## Operating limits

Changed previously imported layers are rejected rather than regenerated. Sources must remain available at their recorded paths. Bounds are not cropped using alpha-channel content. The runtime environment sets any hard total-memory limit; the CLI bounds data batches, submitted jobs, caches, and image dimensions. Documentation includes a pilot procedure and a smaller-memory configuration. Automated tests run without real-world input data.

## Packaging and GitHub Actions preparation

The package/repository/command is now `mbmerge`, with core VERSION still 1.1.0 and unchanged merging semantics. All 22 tests passed on Python 3.12 with Pillow 12.3.0 and again with Pillow 13.0.0; [TEST_RESULTS.txt](TEST_RESULTS.txt) contains both runs.

The wheel and source distribution passed `twine check --strict` and the release identity/content checks. Both direct wheel installation and an isolated rebuild from sdist passed the installed CLI smoke test outside the checkout: real spawned workers, mixed PNG/WebP sources, exact alpha-composited pixels, uniform output format, original bytes for matching unique tiles, metadata, replay and full output verification. An intentionally wrong tag and a preview version marked for production were rejected. Build-staging canaries for unapproved demo outputs and `.env` were excluded from distributions.

Workflow YAML and publishing gates were checked locally. Linux/macOS Python 3.11–3.14 jobs are configured, but GitHub-hosted Actions and actual PyPI/TestPyPI/OIDC publication have not been run in this environment. Repository/account/environment/Trusted Publisher setup remains the owner's step in [RELEASING.md](RELEASING.md). No release or package has been uploaded.
