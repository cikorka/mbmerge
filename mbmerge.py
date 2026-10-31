# SPDX-License-Identifier: MIT
"""Public module entry point; the original script remains compatible.

`python -m mbmerge` and the installed `mbmerge` command share the same core.
Keep VERSION in merge_mbtiles.py as the single source for build/CLI metadata.
"""
from merge_mbtiles import VERSION as __version__, main


if __name__ == "__main__":
    raise SystemExit(main())
