# tools/

Standalone **QGIS Processing algorithms** that complement the plugin — they are
*not* part of the plugin package (the release archive ships only
`basemap_tile_downloader/`), and the plugin never imports them.

The top-level `tools/*.py` here are the Processing algorithms (deployed to a
profile's `processing/scripts/` by `sync.ps1`). One-off **utility scripts** that
are *not* Toolbox algorithms live under [`tools/scripts/`](scripts/) and are
never deployed there — see that folder's note.

Most solve the "discovery" half that the plugin deliberately leaves out: finding
and assembling the remote tiles that cover an area of interest, over `/vsicurl/`,
so nothing is downloaded until you need the pixels. Two of them (`swisstopo`,
`bavaria`) emit a **VRT** you then export with the plugin's **GeoTIFF (local
raster)** backend; the three Austrian ones write a **GeoTIFF** directly. The rest
are post-processing and AOI helpers.

Every AOI-taking script takes the area of interest as a **vector layer** (so the
Toolbox shows the layer's name rather than four coordinates) and defaults to the
active layer. The DTM tools carry their **native resolution and CRS in the
Toolbox name**, and default the Output CRS/Resolution to exactly that — the
defaults resample nothing.

| Script | Toolbox entry | What it does |
| --- | --- | --- |
| `dehaze_ortho.py` | Scripts ▸ Orthophoto | **De-haze & block-match an orthophoto** — make one AOI look like another from the same (or a different) service. Matches per channel on p2/p50/p98 against a reference raster, optionally matching a hazier acquisition block onto a cleaner one across a feathered boundary first. Fixes a uniform haze cast, a warm cast, and a seam *inside* one file. |
| `harmonise_orthophotos.py` | Scripts ▸ Orthophoto | **Harmonise & merge orthophotos (seam colour-match)** — colour-match several overlapping orthophotos (add newest first) and composite them into one seam-reduced GeoTIFF: the plugin's ArcGIS *harmonise flight years*, but for rasters you provide (e.g. Styria's per-period DOP `Flug_2022_2024_RGB` / `_2019_2021_RGB` / `_2016_2018_RGB`, separate ImageServers). Download each period with the plugin, then merge here. Use `dehaze_ortho` instead when the seam is inside a single raster. |
| `reproject_resize_aoi.py` | Scripts ▸ Area of interest | **Reproject & resize AOI (fixed size, aligned to target CRS)** — reproject an AOI's centre to a target CRS and rebuild it as a straight, axis-aligned box of a fixed size (width/height in m, both default 15500). Centre is preserved exactly; output is a one-polygon layer to use as the AOI for the tools below or the plugin. |
| `swisstopo_stac_vrt_algorithm.py` | Scripts ▸ swisstopo | **swisstopo STAC → COG VRT** (swissALTI3D DTM 0.5/2 m, SWISSIMAGE ortho 0.1/2 m, EPSG:2056; Advanced fields override the preset with any other tiled swisstopo COG collection) |
| `bavaria_dgm1_aoi_vrt.py` | Scripts ▸ Germany (Bayern) | **Bavaria DGM1 AOI → VRT (1m 25832)** — Bavaria's open DGM1 (1 m terrain) tiles → a VRT over just the AOI's tiles |
| `tyrol_dgm_aoi.py` | Scripts ▸ Austria | **Tyrol ALS DGM/DOM → DTM GeoTIFF (AOI) (0.5m 31254/31255)** — queries the tiris tile index, downloads each tile's DGM (retryable), then warps the local files to a chosen CRS/resolution. Outputs a GeoTIFF (the two source GK zones are reprojected into one), not a VRT. |
| `austria_bev_dgm_aoi.py` | Scripts ▸ Austria | **Austria BEV ALS DGM/DOM → DTM GeoTIFF (AOI) (1m 3035)** — covers **all of Austria**, including regions with no open per-tile service of their own (e.g. **Upper Austria**). Tiles are COGs on a 50 km EPSG:3035 grid, so only the AOI window is read over `/vsicurl/` (no 50 km download) and warped to a chosen CRS/resolution. The per-tile reference date (Stichtag) is probed automatically. |
| `salzburg_dgm_aoi.py` | Scripts ▸ Austria | **Salzburg DGM → DTM GeoTIFF (AOI) (1m 31258)** — no tile index exists, so the sheet ids (EPSG:31258 grid) are computed from the AOI; each tile is downloaded (retryable) and the local files are warped to a chosen CRS/resolution. (Companion to `scripts/salzburg_dgm1_tiles.py`, the fixed-list CSV generator.) |

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

**VRT tools** (swisstopo, Bavaria):

1. Run the algorithm over your AOI layer → a VRT loads into the project.
2. **Raster ▸ Basemap Tile Downloader** → pick the VRT as the source layer →
   export your exact AOI to a GeoTIFF (reprojected / cropped as usual).

**GeoTIFF tools** (BEV, Tyrol, Salzburg): the output *is* the GeoTIFF — no
export step. Leave Output CRS/Resolution at their defaults to keep the source
grid, or change them to match whatever you are merging with.

**Orthophoto tools**: export each AOI with the plugin first, then run
`dehaze_ortho` (one raster, matched to a reference) or `harmonise_orthophotos`
(several overlapping rasters merged).

## Data licences

- swisstopo swissALTI3D / SWISSIMAGE — © swisstopo, open government data.
- Bavaria DGM1 — © Bayerische Vermessungsverwaltung (BVV), CC BY 4.0.
- Austria BEV ALS DGM/DOM — © BEV (Bundesamt für Eich- und Vermessungswesen),
  CC BY 4.0.
- Tyrol ALS DGM/DOM — © Land Tirol (tiris), CC BY 4.0.
- Salzburg DGM — © Land Salzburg (SAGIS), CC BY 4.0.
