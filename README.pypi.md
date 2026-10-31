# mbmerge

Incrementally merge PNG and WebP raster MBTiles with alpha compositing, bounded batches, parallel image processing, and a single SQLite writer. Resume safely after interruption without applying committed translucent layers twice.

## Installation

```bash
pipx install mbmerge
mbmerge --version
```

Alternatively, install with `python -m pip install mbmerge` in a virtual environment. Requires Python 3.11+, SQLite 3.35+, and Linux/macOS. Windows is not supported. Pillow is installed as a dependency; WebP support is required when reading or writing WebP.

## Usage

```bash
mbmerge merge --input-dir /data/inputs --output /data/merged.mbtiles

# PNG output, including transparent overlaps:
mbmerge merge --input-dir /data/inputs --output /data/merged-png.mbtiles \
  --output-format png --png-compress-level 3

mbmerge status --output /data/merged.mbtiles
mbmerge verify --output /data/merged.mbtiles --decode
```

Run the same merge command again to resume. Completed unchanged sources are skipped; new sources are appended. Later sources are composited over earlier sources at shared tile coordinates. Different tile coordinates remain distinct in the combined archive.

## Input and operating limits

Inputs must be immutable, offline, flat MBTiles tables with a unique ascending index on `(zoom_level,tile_column,tile_row)`. Coordinates are Web Mercator/TMS. Tiles must have the same square dimensions; the default is 512 px (`--tile-size 256` for 256 px sources). Static WebP and PNG up to 8 bits per channel are supported; mixed inputs are converted to a uniform output format. New outputs default to lossless WebP. Animations, 16-bit PNG, JPEG, vector tiles, normalized layouts and WAL-mode sources are rejected.

An existing output created by mbmerge is extended. For an MBTiles archive created by another tool, use it as the first input and choose a new output. Changed previously imported sources are rejected: restore their exact original bytes or rebuild. Keep all recorded source paths available. The output format and active encoder settings are fixed on resume.

Batches, submitted jobs, blob sizes and image dimensions are bounded. The batch budget is not a hard cap on total process RSS. Use local SQLite-compatible storage and avoid long output-reader transactions during importing. Bounds use declared source extents or full tile footprints, without alpha-edge cropping. A real 1 TB import has not been tested.

Use `mbmerge merge --help` for all limits and options. `python -m mbmerge` is also supported.

## License

MIT License. Copyright (c) 2026 mbmerge contributors.
