# mbmerge

Build incremental, lossless raster MBTiles mosaics: **merge one common highest source zoom first, then generate lower zooms from the merged detail**. PNG/WebP alpha compositing, bounded batches, parallel workers, one SQLite writer, and crash-safe recovery of both phases.

## Installation

```bash
pipx install mbmerge
mbmerge --version
```

Or use `python -m pip install mbmerge` in a virtual environment. Requires Linux/macOS, Python 3.11+, SQLite 3.35+ and Pillow (installed as a dependency). Windows is not supported. WebP needs Pillow's WebP codec.

## Usage

```bash
mbmerge merge --input-dir /data/inputs --output /data/mosaic.mbtiles

# Choose a common base level and generate zooms 5..16 as PNG:
mbmerge merge --input-dir /data/inputs --output /data/mosaic-png.mbtiles \
  --base-zoom 16 --min-zoom 5 --output-format png

# Keep supplied zoom artwork instead of generating a pyramid:
mbmerge merge --mode overlay --input-dir /data/inputs --output /data/overlay.mbtiles

mbmerge status --output /data/mosaic.mbtiles
mbmerge verify --output /data/mosaic.mbtiles --decode
```

New outputs default to pyramid mode and lossless WebP. Automatic base selection requires the same actual highest zoom in every nonempty input. An explicit base must exist in every nonempty input. Only the selected level is imported; supplied lower overviews are ignored. After ordered source compositing, lower levels are generated from up to four current children with premultiplied-alpha BOX averaging. Missing children are transparent. On later imports only affected ancestors are rebuilt.

Repeat the merge command after interruption. Completed unchanged sources are skipped. New sources append after earlier layers; an interrupted active source finishes first. Dirty parent keys and their progress are persisted atomically with pixels. Publish only phase `complete`; `verify` rejects incomplete/tainted outputs. During updates readers may see new detail and stale overviews.

## Lossless encoding

PNG and WebP outputs are lossless. WebP explicitly uses `lossless=True, exact=True`; quality=100 alone would not guarantee lossless RGB. Unique matching PNG/lossless WebP tiles may retain their original bytes. Lossy WebP input is transcoded losslessly even without overlap; source losses cannot be restored. Image compositing/downsampling still involves 8-bit rounding and lower spatial resolution.

## Input and operating limits

Inputs must be immutable, offline flat MBTiles with a unique ascending BINARY index on `(zoom_level,tile_column,tile_row)`. Coordinates use the same Web Mercator/TMS grid; square dimensions must match (default 512 px). Static WebP and PNG up to 8 bits/channel can be mixed. Animations, 16-bit PNG, JPEG, vectors, normalized layouts and WAL-mode inputs are rejected. `merge` performs no reprojection. `mbmerge tile` (needs GDAL ≥ 3.11 command-line tools) creates such inputs from GeoTIFF/PDF/VRT/JPEG with polygon clipping, nodata/collar handling and lossless packing. `--reimport changed|always` overlays a new version of an imported source again; `--verify-recorded stat` avoids re-hashing a large unchanged history.

Version 2 uses schema 3 and changes the default to pyramid generation. Existing version 1.x outputs are refused without modification; rebuild to a new output from original sources. Other existing archives can be explicitly used as flattened inputs, never adopted as managed outputs. Mode, base/minimum zoom, format, tile size and active encoder settings are fixed after importing starts.

Changed historical sources are rejected. Keep their recorded paths available; restore exact original bytes or rebuild. Every invocation hashes all historical sources, so use a single input list/directory for the initial collection rather than invoking once per file.

Jobs, blob bytes and image dimensions are bounded; pending parent keys live on disk. The batch budget is not an absolute RSS cap. Pyramid mode requires a byte budget at least five times the maximum tile size in bytes (four children and a parent). Use local SQLite-compatible storage, avoid long output-reader transactions and allow free space for output/WAL/indexes. Bounds use source extents or base tile footprints without alpha cropping. A real 1 TB collection has not been validated.

Use `mbmerge merge --help` for limits. `python -m mbmerge` is supported. The repository contains English/Czech docs, synthetic demos, crash tests and reproducible memory/many-file benchmarks.

## License

MIT License. Copyright (c) 2026 mbmerge contributors.
