# -*- coding: utf-8 -*-
"""Pure-Python engine helpers: log-URL credential masking, and the per-job
cache keys (readable output name + path hash so same-named outputs in different
folders can't wipe each other's queue). Also pins that the ArcGIS fingerprint
does not depend on anything prepare() resolves — the engine fingerprints BEFORE
prepare(), and the dialog's resume check fingerprints freshly-extracted params,
so the two must always agree (the bug this guards against re-showed the
overwrite/ToS prompts on every resume of a harmonised job)."""

import logging
import os

from basemap_tile_downloader import engine
from basemap_tile_downloader.sources import arcgis

# Platform-native paths: the plugin only ever sees paths in the running OS's
# own separators, and os.path.basename doesn't split Windows separators on the
# POSIX CI runners.
PATH_A = os.path.join(os.sep, "jobs", "a", "ortho.tif")
PATH_B = os.path.join(os.sep, "jobs", "b", "ortho.tif")


# ── redact_url ─────────────────────────────────────────────────────────────────
def test_redact_url_masks_credential_params():
    url = "https://wmts.example/service?apikey=SECRET&layer=ortho&token=ALSO"
    red = engine.redact_url(url)
    assert "SECRET" not in red and "ALSO" not in red
    assert "layer=ortho" in red and "apikey=REDACTED" in red


def test_redact_url_without_credentials_is_unchanged():
    for url in ("https://tiles.example/3/2/5.png",
                "https://wms.example/ogc?map=/ms/WMS.map&LAYERS=a,b"):
        assert engine.redact_url(url) == url


def test_redact_url_survives_garbage():
    assert engine.redact_url("not a url at all") == "not a url at all"


# ── cache keys ─────────────────────────────────────────────────────────────────
def test_cache_key_distinct_for_same_basename_in_different_dirs():
    a = engine.cache_key_for(PATH_A, False, "fp")
    b = engine.cache_key_for(PATH_B, False, "fp")
    assert a != b
    assert a.startswith("ortho-") and b.startswith("ortho-")


def test_cache_key_stable_for_same_path():
    assert (engine.cache_key_for(PATH_A, False, "fp")
            == engine.cache_key_for(PATH_A, False, "fp"))


def test_cache_key_temporary_uses_fingerprint():
    assert engine.cache_key_for(None, True, "fp123") == "fp123"
    assert engine.cache_key_for(PATH_A, True, "fp123") == "fp123"


def test_legacy_cache_key_is_the_plain_basename():
    assert engine.legacy_cache_key(PATH_A, False) == "ortho"
    assert engine.legacy_cache_key(None, True) is None


# ── cache accounting (dialog's usage line + purge button) ─────────────────────
def _make_cache(root):
    """A cache tree shaped like a real one: two export folders plus the shared
    tile store."""
    for rel, size in (
            ("job-a/tiles/tile_000001.tif", 1000),
            ("job-a/tiles/tile_000002.tif", 2000),
            ("job-a/tiles.sqlite", 500),
            ("job-b/tiles/tile_000001.tif", 4000),
            ("shared/abc123/18/1/2.tif", 8000),
            ("shared/abc123/18/1/3.tif", 8000)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return 23500


def test_cache_usage_totals_and_split(tmp_path):
    total = _make_cache(tmp_path)
    u = engine.cache_usage(str(tmp_path))
    assert u["done"] and u["total"] == total and u["files"] == 6
    assert u["shared"] == 16000
    assert u["jobs"] == {"job-a": 3500, "job-b": 4000}


def test_cache_usage_missing_root_is_zero(tmp_path):
    u = engine.cache_usage(str(tmp_path / "nope"))
    assert u["done"] and u["total"] == 0 and u["jobs"] == {}


def test_iter_cache_usage_yields_progress_then_final(tmp_path):
    _make_cache(tmp_path)
    # chunk=1 forces a yield per file, so the scan can be stepped by a UI timer.
    steps = list(engine.iter_cache_usage(str(tmp_path), chunk=1))
    assert len(steps) > 1
    assert steps[-1]["done"] and steps[-1]["total"] == 23500


def test_purge_cache_frees_everything(tmp_path):
    root = tmp_path / "__btdcache__"
    root.mkdir()
    total = _make_cache(root)
    freed, errors = engine.purge_cache(str(root))
    assert freed == total and errors == []
    assert not root.exists()


def test_purge_cache_on_missing_root_is_a_no_op(tmp_path):
    assert engine.purge_cache(str(tmp_path / "nope")) == (0, [])


def test_format_size():
    assert engine.format_size(0) == "0 B"
    assert engine.format_size(999) == "999 B"
    assert engine.format_size(1536) == "1.5 KB"
    assert engine.format_size(5 * 1024**3) == "5.0 GB"


# ── metre resolution → request-CRS units (geographic sources must convert) ─────
class _FakeCrs:
    def __init__(self, geographic): self._g = geographic
    def isGeographic(self): return self._g

def test_grid_step_projected_is_metres_unchanged():
    # A projected CRS keeps metres: 1024 px × 0.5 m = 512 map units.
    assert engine.grid_step_units(1024, 0.5, _FakeCrs(False)) == 1024 * 0.5

def test_grid_step_geographic_converts_metres_to_degrees():
    # A geographic CRS divides by metres-per-degree, so a 1 m tile is a fraction
    # of a degree — not 1024° (which produced the "Invalid longitude" / 0×0 warp).
    step = engine.grid_step_units(1024, 1.0, _FakeCrs(True))
    assert step == 1024.0 / engine.METERS_PER_DEGREE
    assert 0 < step < 1.0                       # well under a degree, as it must be


# ── a stale queue (same fingerprint, different tile count) must be rebuilt ─────
def _tiles(n):
    return [{"id": i, "col": i, "row": 0} for i in range(n)]

def test_queue_rebuilt_when_grid_tile_count_changes(tmp_path):
    # An older plugin seeded 1 tile; the fixed grid yields 3 for the SAME
    # fingerprint. The resume must wipe the stale queue and reseed, not keep the 1.
    db = str(tmp_path / "tiles.sqlite")
    meta = {"fingerprint": "abc"}
    q = engine.TileQueue(db, logging.getLogger("t"))
    q.populate_if_empty(_tiles(1), meta)
    assert q.total() == 1
    q.close()

    q = engine.TileQueue(db, logging.getLogger("t"))
    q.populate_if_empty(_tiles(3), meta)          # same fingerprint, new grid
    assert q.total() == 3
    q.close()

def test_queue_resumes_when_tile_count_matches(tmp_path):
    # Same fingerprint and same tile count → genuine resume, queue kept as-is.
    db = str(tmp_path / "tiles.sqlite")
    meta = {"fingerprint": "abc"}
    q = engine.TileQueue(db, logging.getLogger("t"))
    q.populate_if_empty(_tiles(3), meta)
    q.mark_done(0, None)                          # mark one done to prove no wipe
    q.close()

    q = engine.TileQueue(db, logging.getLogger("t"))
    q.populate_if_empty(_tiles(3), meta)
    assert q.total() == 3 and q.counts()["done"] == 1
    q.close()


# ── ArcGIS fingerprint must ignore what prepare() resolves ─────────────────────
ARCGIS_PRE = {"url": "https://gis.example/rest/services/Ortho/MapServer",
              "crs": "EPSG:31254", "format": "png32", "sel_show": None,
              "years": []}


def test_arcgis_fingerprint_ignores_resolved_years():
    opts = {"tile_pixels": 1024, "resolution": 0.5, "harmonize": True}
    post = dict(ARCGIS_PRE, years=[(2024, 5), (2022, 9), (2019, 13)])
    assert (arcgis.fingerprint_parts(ARCGIS_PRE, opts)
            == arcgis.fingerprint_parts(post, opts))


def test_arcgis_fingerprint_still_distinguishes_harmonise():
    base = {"tile_pixels": 1024, "resolution": 0.5}
    assert (arcgis.fingerprint_parts(ARCGIS_PRE, dict(base, harmonize=True))
            != arcgis.fingerprint_parts(ARCGIS_PRE, dict(base, harmonize=False)))


# ── data-coverage report on a finished single-band mosaic ─────────────────────
class _CapturingLog:
    """Records (level, formatted message) so a test can assert on both."""
    def __init__(self):
        self.lines = []

    def _rec(self, level):
        def emit(msg, *args):
            self.lines.append((level, msg % args if args else msg))
        return emit

    def __getattr__(self, level):
        return self._rec(level)


class _FakeBand:
    """Minimal GDAL band: `valid_pct` None means ComputeStatistics raises, which
    is what GDAL does when a band holds no valid pixel at all."""
    def __init__(self, valid_pct):
        self._pct = valid_pct
        self.exact_requested = None

    def ComputeStatistics(self, approx_ok):
        self.exact_requested = approx_ok
        if self._pct is None:
            raise RuntimeError("Failed to compute statistics, no valid pixels found")

    def GetMetadataItem(self, key):
        assert key == "STATISTICS_VALID_PERCENT"
        return str(self._pct)


class _FakeDataset:
    def __init__(self, valid_pct):
        self.band = _FakeBand(valid_pct)

    def GetRasterBand(self, n):
        assert n == 1
        return self.band


def test_data_coverage_is_computed_exactly_not_from_overviews():
    # AVERAGE overviews mark a cell valid when ANY contributing pixel is, so the
    # approximate figure runs far too high on exactly the sparse rasters this is
    # meant to catch. approx_ok must be False.
    ds = _FakeDataset(93.0)
    engine.report_data_coverage(ds, _CapturingLog())
    assert ds.band.exact_requested is False


def test_healthy_coverage_is_logged_not_warned():
    log = _CapturingLog()
    assert engine.report_data_coverage(_FakeDataset(93.0), log) == 93.0
    assert [lvl for lvl, _ in log.lines] == ["info"]


def test_sparse_coverage_warns():
    log = _CapturingLog()
    assert engine.report_data_coverage(_FakeDataset(13.37), log) == 13.37
    (level, msg), = log.lines
    assert level == "warning" and "13.4%" in msg


def test_empty_mosaic_warns_and_reports_zero():
    log = _CapturingLog()
    assert engine.report_data_coverage(_FakeDataset(None), log) == 0.0
    (level, msg), = log.lines
    assert level == "warning" and "NO data" in msg


def test_threshold_boundary_is_not_a_warning():
    log = _CapturingLog()
    engine.report_data_coverage(_FakeDataset(engine.DATA_COVERAGE_WARN_PCT), log)
    assert [lvl for lvl, _ in log.lines] == ["info"]


def test_a_broken_dataset_never_disturbs_the_run():
    class _Exploding:
        def GetRasterBand(self, n):
            raise RuntimeError("dataset closed")

    log = _CapturingLog()
    assert engine.report_data_coverage(_Exploding(), log) is None


# ── annotate_tile_bands: read the real band count / nodata from a tile ─────────
class _FakeBandND:
    def __init__(self, nodata):
        self._nd = nodata

    def GetNoDataValue(self):
        return self._nd


class _FakeTileDS:
    def __init__(self, bands, nodata):
        self.RasterCount = bands
        self._nd = nodata

    def GetRasterBand(self, n):
        assert n == 1
        return _FakeBandND(self._nd)


class _FakeGdal:
    """Minimal gdal module: Open returns a preset dataset (None to simulate an
    unreadable tile)."""
    def __init__(self, ds):
        self._ds = ds

    def Open(self, path):
        return self._ds


def test_annotate_records_single_band_nodata(monkeypatch):
    monkeypatch.setattr(engine, "gdal", _FakeGdal(_FakeTileDS(1, -99999.0)))
    params = {}
    engine.annotate_tile_bands(params, "tile.tif", _CapturingLog())
    assert params["_tile_bands"] == 1 and params["_tile_nodata"] == -99999.0


def test_annotate_rgb_sets_band_count_without_nodata(monkeypatch):
    monkeypatch.setattr(engine, "gdal", _FakeGdal(_FakeTileDS(3, None)))
    params = {}
    engine.annotate_tile_bands(params, "tile.tif", _CapturingLog())
    assert params["_tile_bands"] == 3 and "_tile_nodata" not in params


def test_annotate_is_a_noop_without_gdal(monkeypatch):
    monkeypatch.setattr(engine, "gdal", None)
    params = {}
    engine.annotate_tile_bands(params, "tile.tif", _CapturingLog())
    assert params == {}


def test_annotate_is_a_noop_on_an_unreadable_tile(monkeypatch):
    monkeypatch.setattr(engine, "gdal", _FakeGdal(None))    # Open() -> None
    params = {}
    engine.annotate_tile_bands(params, "tile.tif", _CapturingLog())
    assert params == {}
