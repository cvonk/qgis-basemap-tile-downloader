# CLAUDE.md

Guidance for working in this repository. Keep it accurate: when a change makes a
statement here wrong, update the statement in the same change. Adding or removing
a source backend is enforced — CI (`docs-guard`) fails such a change unless it
also touches this file.

## What this is

A QGIS plugin (`Raster ▸ Basemap Tile Downloader…`) that downloads a WMS, WMTS,
WCS, XYZ, or ArcGIS REST basemap — or exports a local GDAL raster — over a chosen
extent and mosaics it into a single compressed, overview-tiled GeoTIFF, with
adaptive throttling, a resumable SQLite queue, and a cross-job shared tile cache.
Pure Python; the only heavy dependency is the GDAL bindings that ship with QGIS.

## Layout

- `basemap_tile_downloader/` — the plugin package (this is what ships).
  - `engine.py` — source-agnostic core: blocking HTTP, adaptive throttle,
    SQLite work queue, tile georeferencing, GDAL mosaicking, and the `QgsTask`
    that drives a run. The big one; start here.
  - `plugin.py` — QGIS entry point, menu action, dialog wiring, the completion
    message bar.
  - `dialog.py` — the settings dialog. Shows only the fields that apply to the
    selected source; also the cache-usage display and Clear-cache button.
  - `sources/` — one module per backend, all matching a common contract (below):
    `xyz.py`, `wmts.py`, `wms.py`, `wcs.py`, `arcgis.py`, `gdal_raster.py`.
  - `tilemath.py` — pure Web-Mercator XYZ tile math, deliberately QGIS-free so
    it unit-tests standalone.
  - `safexml.py` — hardened XML parsing (entities disabled) for all untrusted
    server XML. defusedxml is **not** bundled with QGIS, hence this homegrown
    replacement; never parse server XML with `xml.etree.ElementTree` directly.
  - `metadata.txt` — plugin manifest. `version=` must match the release tag, and
    `changelog=` gets a new top entry per release.
- `tests/` — pytest suite. `conftest.py` installs minimal `qgis.*` stubs so the
  logic tests import and run under plain Python; under a real QGIS Python the
  genuine modules are used and nothing is stubbed.
- `.github/workflows/` — `ci.yml` (every push/PR) and `release.yml` (on a `v*`
  tag). `.github/scripts/validate_metadata.py` checks the manifest.

## The source-backend contract

Each `sources/*.py` module exposes a standard surface that `engine.py` dispatches
to; the authoritative, commented list is the module docstring at the top of
`engine.py`. Required: `SOURCE_NAME`, `detect(layer)`, `extract_params(layer)`,
`native_crs`, `default_out_crs`, `build_tile_grid`, `fetch_one_tile`,
`fingerprint_parts`. Optional hooks a source may add: `prepare` (pre-run network
setup, e.g. capabilities negotiation), `LOCAL`, `CONCURRENCY`,
`INITIAL_DELAY_SEC`, `SHAREABLE` + `shared_signature` + `shared_rel_path`
(shared-cache identity), `mosaic_hints` (alpha vs nodata), `compose_mosaic`
(take over the mosaic step).

`source_for(layer)` in `engine.py` runs each backend's `detect()` in order and
returns the first match. `detect()` keys off `layer.providerType()`: `"wms"` is
shared by WMS/WMTS/XYZ (disambiguated by URI params — XYZ carries `type=xyz`,
WMTS carries `tileMatrixSet`, else WMS), `"wcs"` is WCS, `"arcgismapserver"` is
ArcGIS, `"gdal"` is a local raster.

When adding a backend, wire it in three places: the import+tuple in
`_source_modules()`, and the `name in (...)` branches in `dialog.py` (field
visibility in `_on_layer_changed`, the tile estimate in `_estimate_tiles`, and
the opts dict in `values()`). Add response-classification tests to
`tests/test_fetch_classification.py` and a backend-specific module like
`tests/test_wcs.py`. Update the backend list above in this file — CI's
`docs-guard` job blocks a backend add/remove that leaves this file untouched
(`.github/scripts/require_claude_md_update.py`).

## Conventions that trip you up

- **Compact style is deliberate.** Aligned assignments, inline comments,
  grouped imports, one-line `if`s and semicolon statements. ruff runs pyflakes
  only (`select = ["F"]`) and flake8 ignores the style codes — see `ruff.toml`
  and `.flake8` for exactly which, and why. Match the surrounding code; don't
  "clean up" the style.
- **Line length is 100**, enforced by ruff. flake8's E501 is off so length
  isn't double-checked.
- **HTTP status ≥ 400 also sets an error string** (`QgsBlockingNetworkRequest`
  reports it as `ServerExceptionError`), so in every `fetch_one_tile`, the
  status-specific branches (429/500/503 throttle, Retry-After) MUST come before
  the generic `if err:` raise, or the throttle paths are unreachable. This has
  regressed before; `tests/test_fetch_classification.py` pins it.
- **Float rasters need no TIFF predictor.** `PREDICTOR=2` is byte-wise
  differencing, wrong for Float32 (DTM) samples; use predictor 3 or none.
  `engine.georeference()` takes `creation_options` for this — WCS and the local
  raster backend pass plain `COMPRESS=DEFLATE`.
- **Statistics for the data-coverage check are computed exactly**, never from
  overviews — AVERAGE overviews mark a cell valid when any pixel is, reading
  ~55% on a mosaic that is 13% real data. See `report_data_coverage`.
- `# nosec BXXX` markers are intentional and scoped; bandit runs in CI at full
  severity, so keep them.

## Build / test / run

Run the **full** CI gate locally before pushing — all of it, in this order,
because CI fails on any one and the last two are easy to skip by habit:

```bash
python -m ruff check .
python -m flake8 basemap_tile_downloader tests
python -m bandit -r basemap_tile_downloader -q
python -m detect_secrets scan | python -c "import json,sys; r=json.load(sys.stdin)['results']; sys.exit('secrets: '+', '.join(r) if r else 0)"
python -m pytest -q
python .github/scripts/validate_metadata.py
```

`detect-secrets` is the one with no local muscle memory and it has bitten a
release: an assertion shaped like `apikey == "literal"` reads as a hard-coded
credential. Assert credential-shaped params against the built URL string
instead (`assert "apikey=…" in url`).

CI additionally runs a `docs-guard` job (`require_claude_md_update.py`) that
fails a change adding or removing a `sources/*.py` backend without touching this
file — a diff-range check, so nothing to run locally, but expect it to fail a
new-backend PR until you update the backend list here.

The plain-Python run above uses the `qgis.*` stubs. To exercise real
QGIS/GDAL code paths, run under the bundled interpreter:
`& "U:\Program Files\QGIS 3.44.11\bin\python-qgis-ltr.bat" -m pytest -q`.

**Seeing a change in QGIS:** QGIS loads the plugin from *installed copies* under
each profile root, not from this repo. After editing, run `sync.ps1` (copies the
package into every QGIS3/QGIS4 profile it finds), then reload the plugin in QGIS.
Repo edits are invisible until synced.

## Releasing

`release.yml` fires on a `v*` tag: it `git archive`s the `basemap_tile_downloader/`
folder into `basemap_tile_downloader-<tag>.zip` and publishes a GitHub release.
So a release is:

1. Bump `version=` in `metadata.txt` and add a `changelog=` entry.
2. Run the full CI gate above.
3. Commit, `git tag -a vX.Y.Z -m "Release X.Y.Z"`, `git push --follow-tags`.
4. Run `sync.ps1` to install the new version locally.

Tag `vX.Y.Z` must equal `metadata.txt`'s `version=`. Tests aren't shipped (the
archive is package-only), but everything else in the package is, so keep the
tree green at the tagged commit. `gh` is not on PATH here — invoke it by full
path, `U:\Program Files\GitHub CLI\gh.exe`.
