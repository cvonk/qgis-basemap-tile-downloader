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
| `measure_sun_from_ortho.py` | `"<QGIS>/bin/python-qgis-ltr.bat" measure_sun_from_ortho.py --csv sun_positions.csv` | Recover the sun azimuth/elevation an orthophoto was flown under, per AOI, so the Blender sun can match the shadows baked into the texture. Matches **cast shadows** via horizon-angle scanning — plain hillshade correlation fails in the Alps, where aspect correlates with albedo. `--list` shows the DTM/ortho pairs; `--synthetic AZ,EL` renders the DTM at a known sun and checks it recovers it (the correctness gate — run it after any change). Flags `WEAK`/`UNRELIABLE` rather than printing a confident wrong number. |

All four need PyQGIS + the GDAL Python bindings that QGIS ships, nothing else.

## The Blender and video-delivery scripts live with the .blend files

Everything downstream of QGIS — the Blender scripts and the EXR-to-HDR10 encode
chain — was moved out to the video project, next to the `.blend` files and the
ffmpeg build it all uses:

    …\Graphics\3D\Blender\tools\scripts\
        enforce_output_colorspace.py   pin the EXR output space to Linear Rec.2020
        reset_blend_for_new_aoi.py     strip a .blend copy back to its reusable shell
        encode_hdr_from_exr.ps1        EXR sequence -> HDR10 (PQ / BT.2020) H.265 MP4
        measure_maxcll.py              measure the sequence's true MaxCLL / MaxFALL

They are documented in that project's `HOWTO.txt`, which is the master procedure
doc for the terrain-flythrough pipeline. The split is by dependency: what is left
here needs QGIS, what moved needs Blender or ffmpeg (`Blender\tools\` holds both
`ffmpeg.exe` and `ffprobe.exe`).
