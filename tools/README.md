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
| `harmonise_orthophotos.py` | Scripts ▸ Orthophoto | Colour-match several overlapping orthophotos (add newest first) and composite them into one seam-reduced GeoTIFF — the plugin's ArcGIS *harmonise flight years*, but for rasters you provide (e.g. Styria's per-period DOP `Flug_2022_2024_RGB` / `_2019_2021_RGB` / `_2016_2018_RGB`, which are separate ImageServers). Download each period with the plugin, then merge here. |
| `reproject_resize_aoi.py` | Scripts ▸ Area of interest | Reproject an AOI's centre to a target CRS and rebuild it as a straight, axis-aligned box of a fixed size (width/height in m, both default 16500). Centre is preserved exactly; output is a one-polygon layer to use as an export extent. |
| `swisstopo_stac_vrt_algorithm.py` | Scripts ▸ swisstopo | swisstopo STAC → COG VRT (swissALTI3D DTM 0.5/2 m, SWISSIMAGE ortho 0.1/2 m; `--collection` override for other tiled swisstopo COG collections) |
| `bavaria_dgm1_aoi_vrt.py` | Scripts ▸ Germany (Bayern) | Bavaria open DGM1 (1 m terrain) tiles → VRT over just the AOI's tiles |
| `tyrol_dgm_aoi.py` | Scripts ▸ Austria | Tyrol (tiris) ALS DGM/DOM → a DTM GeoTIFF for an AOI: queries the tile index, downloads each tile's DGM (retryable), then warps the local files to a chosen CRS/resolution. Outputs a GeoTIFF (mixed source zones are reprojected), not a VRT. |
| `austria_bev_dgm_aoi.py` | Scripts ▸ Austria | BEV nationwide ALS DGM/DOM (1 m) → a DTM GeoTIFF for an AOI — covers **all of Austria**, including regions with no open per-tile service of their own (e.g. **Upper Austria**). Tiles are COGs on a 50 km EPSG:3035 grid, so only the AOI window is read over `/vsicurl/` (no 50 km download) and warped to a chosen CRS/resolution. The per-tile reference date (Stichtag) is probed automatically. |
| `salzburg_dgm_aoi.py` | Scripts ▸ Austria | Salzburg open DGM1 (1 m) → a DTM GeoTIFF for an AOI. No tile index exists, so the sheet ids (EPSG:31258 grid) are computed from the AOI; each tile is downloaded (retryable) and the local files are warped to a chosen CRS/resolution. (Companion to `scripts/salzburg_dgm1_tiles.py`, the fixed-list CSV generator.) |

> The two **provincial** Austria tools (Tyrol, Salzburg) **download tiles to a
> temp dir first, then warp the local files** — those servers are slow and drop
> connections under sustained load, which aborts a long streaming warp.
> Downloading first (with per-tile retry) makes large AOIs reliable; it also
> means most of the runtime is the download, so the progress bar is meaningful
> throughout. The **BEV** tool is different: its tiles are COGs, so it reads only
> the AOI window over `/vsicurl/` (GDAL retries each small range request) and
> warps that — no whole-tile download, and reliable on BEV's national server.

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
