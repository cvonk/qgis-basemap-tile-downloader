# -*- coding: utf-8 -*-
"""WMS GetMap URL construction, focused on the 1.3.0 BBOX axis-order rule.

Regression cover for a Copernicus VHR 2021 failure: the server advertises
CRS:84 first, QGIS picked it up as the layer CRS, and the job died on
"Request CRS 'CRS:84' is invalid" because QGIS only knows that CRS as
OGC:CRS84. The same hand-maintained list also had CRS:84 marked as axis-swapped
(it is the lon/lat one, so it must not be) and omitted northing-first projected
CRSs like EPSG:3035, which fail silently with a blank tile rather than an error.
"""
import urllib.parse

import pytest

from basemap_tile_downloader.sources import wms


def _real_qgis():
    from qgis.core import QgsCoordinateReferenceSystem
    return QgsCoordinateReferenceSystem("EPSG:4326").isValid()


try:
    HAVE_QGIS = _real_qgis()
except Exception:                                        # noqa: BLE001
    HAVE_QGIS = False

needs_qgis = pytest.mark.skipif(not HAVE_QGIS, reason="needs the real QGIS CRS database")

PARAMS = {"url": "https://example.test/wms", "layers": ["L"], "styles": [""],
          "crs": "EPSG:4326", "format": "image/png", "extra": {}}
TILE = {"id": 0, "col": 1, "row": 2,
        "xmin": 11.0, "ymin": 46.0, "xmax": 12.0, "ymax": 47.0}


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# ── axis-order decision ──────────────────────────────────────────────────────
def test_crs84_is_never_axis_swapped():
    # CRS:84 exists precisely to be lon/lat; swapping it silently mirrors the AOI.
    assert wms._axis_inverted("CRS:84") is False
    assert wms._axis_inverted("crs:84") is False
    assert wms._axis_inverted("OGC:CRS84") is False


def test_epsg4326_is_axis_swapped():
    assert wms._axis_inverted("EPSG:4326") is True


@needs_qgis
def test_axis_order_comes_from_qgis_not_a_hardcoded_list():
    # The bug: EPSG:3035 is northing/easting but was absent from the old list.
    assert wms._axis_inverted("EPSG:3035") is True
    assert wms._axis_inverted("EPSG:4258") is True
    assert wms._axis_inverted("EPSG:32632") is False
    assert wms._axis_inverted("EPSG:3857") is False


# ── the spelling QGIS/GDAL get to see ────────────────────────────────────────
@needs_qgis
def test_crs84_is_translated_for_qgis():
    # QgsCoordinateReferenceSystem("CRS:84") is invalid — this is what used to
    # abort the whole job before a single tile was requested.
    assert wms._qgis_crs("CRS:84").isValid()
    assert wms._authid("CRS:84") == "OGC:CRS84"
    assert wms.native_crs({"crs": "CRS:84"}, {}) == "OGC:CRS84"
    assert wms.default_out_crs({"crs": "CRS:84"}) == "OGC:CRS84"


@needs_qgis
def test_ordinary_crs_passes_through_unchanged():
    assert wms._authid("EPSG:3035") == "EPSG:3035"
    assert wms.native_crs({"crs": "EPSG:32632"}, {}) == "EPSG:32632"


# ── the resulting GetMap BBOX ────────────────────────────────────────────────
def test_bbox_is_swapped_for_epsg4326_in_130():
    q = _query(wms._getmap_url(PARAMS, {"tile_pixels": 512}, TILE))
    assert q["CRS"] == "EPSG:4326"
    assert q["BBOX"] == "46.0,11.0,47.0,12.0"


def test_bbox_is_not_swapped_for_crs84_and_keeps_the_wire_spelling():
    p = dict(PARAMS, crs="CRS:84")
    q = _query(wms._getmap_url(p, {"tile_pixels": 512}, TILE))
    assert q["BBOX"] == "11.0,46.0,12.0,47.0"
    assert q["CRS"] == "CRS:84"          # send back exactly what was advertised


@needs_qgis
def test_bbox_is_swapped_for_northing_first_projected_crs():
    p = dict(PARAMS, crs="EPSG:3035")
    t = dict(TILE, xmin=4453564.0, ymin=2610460.0, xmax=4454564.0, ymax=2611460.0)
    q = _query(wms._getmap_url(p, {"tile_pixels": 512}, t))
    assert q["BBOX"] == "2610460.0,4453564.0,2611460.0,4454564.0"


def test_bbox_is_never_swapped_in_111():
    p = dict(PARAMS, url="https://example.test/wms?VERSION=1.1.1")
    q = _query(wms._getmap_url(p, {"tile_pixels": 512}, TILE))
    assert q["BBOX"] == "11.0,46.0,12.0,47.0"
    assert q["SRS"] == "EPSG:4326" and "CRS" not in q
