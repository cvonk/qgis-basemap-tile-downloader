# tools/scripts/

One-off **utility scripts**, kept under version control for reference/reuse. Unlike
the Processing algorithms in the parent `tools/` folder, these are **not** QGIS
Toolbox algorithms and are **not** deployed to a profile's `processing/scripts/`
by `sync.ps1` (it only globs the top-level `tools/*.py`). Keeping them out of
`processing/scripts/` matters: `resize_aois.py` runs its logic at import, so QGIS
would execute it on every launch if it were installed there.

They are project-specific (a Dolomites 3-D/terrain project) — treat them as
worked examples to adapt, not general tools.

| Script | How to run | What it does |
| --- | --- | --- |
| `resize_aois.py` | QGIS **Python Console**: `exec(open(r"<path>/resize_aois.py", encoding="utf-8").read())` | Resize every AOI polygon layer in the open project to a square about its current centre (edit `SIZE_EW`/`SIZE_NS`). |
| `crop_aois.py` | Terminal (QGIS Python), **QGIS closed**: `python crop_aois.py --project "<path>.qgz"` | Crop every per-AOI GeoTIFF the project references to its AOI square, in place, backing up originals once into `_backup_precrop/`. `--dry-run` to preview; re-runnable (crops from the pristine backup). |

Both need only PyQGIS + the GDAL Python bindings that QGIS ships.
