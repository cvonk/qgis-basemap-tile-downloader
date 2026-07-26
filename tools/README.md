# tools/

Standalone **QGIS Processing algorithms** that complement the plugin — they are
*not* part of the plugin package (the release archive ships only
`basemap_tile_downloader/`), and the plugin never imports them.

The top-level `tools/*.py` here are the Processing algorithms (deployed to a
profile's `processing/scripts/` by `sync.ps1`). One-off **utility scripts** that
are *not* Toolbox algorithms live under [`tools/scripts/`](scripts/) and are
never deployed there — see that folder's note.

They solve the "discovery" half that the plugin deliberately leaves out: finding
and assembling the remote tiles that cover an area of interest into a single
virtual raster (VRT), which you then export with the plugin's **GeoTIFF (local
raster)** backend. Each builds the VRT over `/vsicurl/` sources, so nothing is
downloaded until you export — the VRT streams only the pixels it reads.

| Script | Toolbox entry | What it does |
| --- | --- | --- |
| `swisstopo_stac_vrt_algorithm.py` | Scripts ▸ swisstopo | swisstopo STAC → COG VRT (swissALTI3D DTM 0.5/2 m, SWISSIMAGE ortho 0.1/2 m; `--collection` override for other tiled swisstopo COG collections) |
| `bavaria_dgm1_aoi_vrt.py` | Scripts ▸ Germany (Bayern) | Bavaria open DGM1 (1 m terrain) tiles → VRT over just the AOI's tiles |
| `tyrol_dgm_aoi.py` | Scripts ▸ Austria | Tyrol (tiris) ALS DGM/DOM → a DTM GeoTIFF for an AOI: queries the tile index, reads the DGM inside each remote ZIP via `/vsizip//vsicurl/`, warps to a chosen CRS/resolution. Outputs a GeoTIFF (mixed source zones are reprojected), not a VRT. |

## Install

Copy the file into your QGIS profile's Processing scripts folder, then it appears
in the Toolbox (restart QGIS or use *Reload Scripts*):

```
<profile>/processing/scripts/
# Windows: %APPDATA%\QGIS\QGIS3\profiles\default\processing\scripts\
```

Or in QGIS: **Processing Toolbox ▸ Scripts (top icon) ▸ Add Script to Toolbox**,
and pick the file.

They need only what QGIS already ships (PyQGIS + the GDAL Python bindings).

## Typical flow

1. Run the algorithm over your AOI → a VRT loads into the project.
2. **Raster ▸ Basemap Tile Downloader** → pick the VRT as the source layer →
   export your exact AOI to a GeoTIFF (reprojected / cropped as usual).

## Data licences

- swisstopo swissALTI3D / SWISSIMAGE — © swisstopo, open government data.
- Bavaria DGM1 — © Bayerische Vermessungsverwaltung (BVV), CC BY 4.0.
