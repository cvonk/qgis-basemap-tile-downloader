"""Unit tests for the pure Web-Mercator tile math (no QGIS/GDAL needed).

Run from the repo root:  pytest
"""
import pytest

from basemap_tile_downloader import tilemath as tm


def test_resolution_halves_each_zoom():
    assert tm.tile_resolution_m(1) == tm.tile_resolution_m(0) / 2
    assert tm.tile_resolution_m(10) == tm.tile_resolution_m(9) / 2


def test_resolution_matches_formula():
    for z in (0, 5, 18, 22):
        assert tm.tile_resolution_m(z) == (2.0 * tm.WM_ORIGIN) / (2 ** z) / tm.TILE_PIXELS


def test_zoom18_resolution_approx():
    # Classic ~0.597 m/px at zoom 18 (equator).
    assert abs(tm.tile_resolution_m(18) - 0.5971642835) < 1e-6


def test_resolution_at_lat_equator_matches_equatorial():
    for z in (0, 12, 18):
        assert tm.tile_resolution_m_at_lat(z, 0.0) == tm.tile_resolution_m(z)


def test_resolution_at_lat_60_is_half():
    # cos(60°) = 0.5
    assert abs(tm.tile_resolution_m_at_lat(18, 60.0)
               - tm.tile_resolution_m(18) * 0.5) < 1e-9


def test_resolution_at_lat_finer_towards_poles():
    # Ground resolution number gets smaller (finer) away from the equator.
    assert tm.tile_resolution_m_at_lat(18, 46.6) < tm.tile_resolution_m(18)


def test_zoom_for_resolution_inverts_resolution_at_lat():
    for z in (5, 12, 16, 18):
        for lat in (0.0, 46.5, 60.0):
            r = tm.tile_resolution_m_at_lat(z, lat)
            assert abs(tm.zoom_for_resolution_m(r, lat) - z) < 1e-9


def test_zoom_for_resolution_without_lat_is_equatorial():
    assert abs(tm.zoom_for_resolution_m(tm.tile_resolution_m(14)) - 14) < 1e-9


def test_zoom_for_resolution_lat_gives_coarser_zoom():
    # A TRUE 2 m at 46.5 deg is a nominal ~2.9 m in Web Mercator, so it matches a
    # LOWER (coarser) zoom than 2 m read as an equatorial figure.
    assert tm.zoom_for_resolution_m(2.0, 46.5) < tm.zoom_for_resolution_m(2.0, 0.0)


def test_zoom_for_resolution_dolomites_2m():
    # The case this was added for: 2 m in the Dolomites sits between zoom 15 and
    # 16, nearest 16.
    z = tm.zoom_for_resolution_m(2.0, 46.5)
    assert 15.0 < z < 16.0
    assert round(z) == 16


def test_zoom_for_resolution_rejects_bad_input():
    with pytest.raises(ValueError):
        tm.zoom_for_resolution_m(0.0)
    with pytest.raises(ValueError):
        tm.zoom_for_resolution_m(-1.0)
    with pytest.raises(ValueError):
        tm.zoom_for_resolution_m(1.0, 90.0)


def test_tile_bounds_z0_covers_world():
    ulx, uly, lrx, lry = tm.tile_bounds_3857(0, 0, 0)
    assert ulx == -tm.WM_ORIGIN
    assert uly == tm.WM_ORIGIN
    assert lrx == tm.WM_ORIGIN
    assert lry == -tm.WM_ORIGIN


def test_tile_bounds_z1_quadrants():
    # Upper-left tile (0,0)
    assert tm.tile_bounds_3857(0, 0, 1) == (-tm.WM_ORIGIN, tm.WM_ORIGIN, 0.0, 0.0)
    # Lower-right tile (1,1)
    assert tm.tile_bounds_3857(1, 1, 1) == (0.0, 0.0, tm.WM_ORIGIN, -tm.WM_ORIGIN)


def test_xyz_url_basic():
    assert tm.xyz_url("https://s/{z}/{x}/{y}.png", 3, 5, 7) == "https://s/7/3/5.png"


def test_xyz_url_tms_flip():
    # zoom 2 -> n=4; {-y} for y=1 is 4-1-1 = 2
    assert tm.xyz_url("https://s/{z}/{x}/{-y}", 0, 1, 2) == "https://s/2/0/2"


def test_tile_range_center_maps_back():
    # The centre of a known tile must resolve back to exactly that tile.
    z, x, y = 12, 2200, 1500
    ulx, uly, lrx, lry = tm.tile_bounds_3857(x, y, z)
    cx, cy = (ulx + lrx) / 2, (uly + lry) / 2
    assert tm.tile_range(cx, cy, cx, cy, z) == (x, x, y, y)


def test_tile_range_spans_multiple_tiles():
    z = 10
    # A bbox spanning tiles (100..102) x (200..201)
    ulx0, uly0, _, _ = tm.tile_bounds_3857(100, 200, z)
    _, _, lrx1, lry1 = tm.tile_bounds_3857(102, 201, z)
    xmin, xmax, ymin, ymax = tm.tile_range(ulx0 + 1, lry1 + 1, lrx1 - 1, uly0 - 1, z)
    assert (xmin, xmax, ymin, ymax) == (100, 102, 200, 201)


def test_tile_range_clamped_to_grid():
    z = 3
    xmin, xmax, ymin, ymax = tm.tile_range(-9e7, -9e7, 9e7, 9e7, z)
    assert (xmin, ymin) == (0, 0)
    assert (xmax, ymax) == (2 ** z - 1, 2 ** z - 1)


def test_wms_grid_dims_exact():
    assert tm.wms_grid_dims(1000, 500, 100) == (10, 5)


def test_wms_grid_dims_rounds_up():
    assert tm.wms_grid_dims(1050, 450, 100) == (11, 5)


def test_wms_grid_dims_at_least_one():
    assert tm.wms_grid_dims(10, 10, 100) == (1, 1)


def test_wms_grid_dims_bad_step():
    try:
        tm.wms_grid_dims(100, 100, 0)
    except ValueError:
        return
    assert False, "expected ValueError for step <= 0"
