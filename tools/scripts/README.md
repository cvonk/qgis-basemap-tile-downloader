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
| `salzburg_dgm1_tiles.py` | `python salzburg_dgm1_tiles.py` | Write `salzburg_dgm1_tiles.csv` — the download URLs for a fixed set of Salzburg DGM1 (1 m LiDAR) tiles from the province's open ALS archive. Feed the CSV to aria2c (`aria2c -c -x 16 -s 16 -i salzburg_dgm1_tiles.csv`), then merge in QGIS. Edit the `IDS` list for other sheets. |
| `encode_hdr_from_exr.ps1` | PowerShell: `.\encode_hdr_from_exr.ps1 -InputDir "<...>\Rendered\Seceda"` | Encode a Blender scene-linear OpenEXR sequence to an HDR10 (PQ / BT.2020) 10-bit H.265 MP4 via ffmpeg. Reads each frame's `colorInteropID` to pick `primariesin`, auto-detects the frame range, warns on gaps and refuses a folder mixing colour spaces. `-Preview 120` for a quick look, `-WhatIf` to print the ffmpeg command without running it. Needs ffmpeg with `zscale` and `libx265`. |
| `measure_maxcll.py` | `"<Blender>/python/bin/python.exe" measure_maxcll.py --input "<...>\Seceda\%04d.exr" --start 50 --ffmpeg "<...>\ffmpeg.exe"` | Measure the sequence's true MaxCLL / MaxFALL (CTA-861.3) so the HDR10 metadata describes the actual content instead of x265's `1000,400` guess. Prints the flags to pass to `encode_hdr_from_exr.ps1`. Needs numpy — use Blender's bundled Python. |

Requirements differ per script:

- `resize_aois.py`, `crop_aois.py`, `salzburg_dgm1_tiles.py` — PyQGIS + the GDAL
  Python bindings that QGIS ships, nothing else.
- `encode_hdr_from_exr.ps1` — ffmpeg built with `zscale` and `libx265`. It is not
  on PATH here, so pass `-FFmpeg <path>`.
- `measure_maxcll.py` — ffmpeg/ffprobe and **numpy**; no QGIS. Run it with
  Blender's bundled Python, which has numpy.
