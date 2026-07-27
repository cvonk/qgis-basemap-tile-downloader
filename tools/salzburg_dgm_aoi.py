# -*- coding: utf-8 -*-
"""
Salzburg DGM AOI → DTM GeoTIFF — a QGIS Processing algorithm.

Builds a terrain (DGM1, 1 m) GeoTIFF for a chosen extent from Salzburg's open
LiDAR tiles, without a manual tile download. The tiles are direct GeoTIFFs on the
province's open archive, 2500 m each, 1 m, Float32, in EPSG:31258 (Austria GK
Central). There is no tile-index service, so the sheet IDs are computed from the
AOI grid and each candidate is probed (missing = outside Salzburg). The DGMs are
read over /vsicurl/ and warped to your CRS and resolution, cropped to the AOI.

Data © Land Salzburg (SAGIS), CC BY 4.0.

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸ Austria.
"""
import math
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterExtent, QgsProcessingParameterNumber,
    QgsProcessingParameterCrs, QgsProcessingParameterRasterDestination,
    QgsCoordinateReferenceSystem,
)

try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
except ImportError:      # pragma: no cover
    gdal = None

BASE = ("https://service.salzburg.gv.at/sagisogd/archiv/raster/hoehen/"
        "laserscan/Rasterpunkte/DGM/DGM_1")
TILE_CRS = "EPSG:31258"
TILE = 2500.0                      # tile side, metres
SRC_NODATA = -3.4028234663852886e+38   # the archive's nodata (min float)
DST_NODATA = -9999.0
MAX_TILES = 1200                   # ~7500 km²; guard against an over-large AOI


def _download(url, dst, feedback, tries=5):
    """Download `url` to `dst`, retrying transient failures. Returns dst on
    success, None on a 404 (tile doesn't exist) or after exhausting retries.
    Downloading first — rather than streaming each tile into gdal.Warp — is what
    makes a large run reliable: this server is slow and drops connections under
    sustained load, and a dropped read mid-warp loses the whole (long) warp."""
    for attempt in range(tries):
        if feedback.isCanceled():
            return None
        try:
            with urllib.request.urlopen(url, timeout=180) as r, \
                    open(dst, "wb") as f:                       # nosec B310 (https)
                shutil.copyfileobj(r, f, 1 << 20)
            if gdal.Open(dst) is not None:
                return dst
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:      # noqa: BLE001  (transient network error → retry)
            pass
        time.sleep(2 * (attempt + 1))
    return None


def sheet_id(te, tn):
    """Salzburg DGM sheet id for the 2500 m tile whose SW corner is (te, tn) in
    EPSG:31258. Id = <E_10km+1><N_10km>5<h>0<u>, where h/u are the 5 km / 2.5 km
    quadtree quadrants within the 10 km base sheet (bit0=east, bit1=south)."""
    ul_y = tn + TILE
    e10 = int(math.floor(te / 10000.0))
    n10 = int(math.ceil(ul_y / 10000.0))
    e_off = te - e10 * 10000            # 0 / 2500 / 5000 / 7500
    n_off = n10 * 10000 - ul_y          # from the sheet's top edge
    h = (1 if e_off >= 5000 else 0) + 2 * (1 if n_off >= 5000 else 0)
    u = (1 if e_off % 5000 >= 2500 else 0) + 2 * (1 if n_off % 5000 >= 2500 else 0)
    return f"{e10 + 1}{n10}5{h}0{u}"


class SalzburgDgmAoi(QgsProcessingAlgorithm):
    AOI = "AOI"
    RES = "RES"
    CRS = "CRS"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return SalzburgDgmAoi()

    def name(self):
        return "salzburg_dgm_aoi"

    def displayName(self):
        return "Salzburg DGM → DTM GeoTIFF (AOI)"

    def group(self):
        return "Austria"

    def groupId(self):
        return "austria"

    def shortHelpString(self):
        return (
            "Build a 1 m terrain (DGM) GeoTIFF for the chosen extent from "
            "Salzburg's open LiDAR tiles, without downloading tiles by hand.\n\n"
            "The 2500 m tiles (EPSG:31258, 1 m, Float32) are read over /vsicurl/ "
            "and warped to your CRS and resolution, cropped to the AOI. There is "
            "no tile index, so sheet ids are computed from the AOI grid and each "
            "is probed — ones outside Salzburg simply don't exist and are skipped. "
            "Data © Land Salzburg (SAGIS), CC BY 4.0."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(
            self.AOI, "Area of interest (any CRS)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.RES, "Output resolution (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=1.0, maxValue=50.0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Output CRS", defaultValue="EPSG:32632"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, "Output DTM"))

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None:
            raise QgsProcessingException("GDAL Python bindings are unavailable.")
        res = self.parameterAsDouble(parameters, self.RES, context)
        out_crs = self.parameterAsCrs(parameters, self.CRS, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        rect = self.parameterAsExtent(
            parameters, self.AOI, context, QgsCoordinateReferenceSystem(TILE_CRS))
        if rect.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")

        e0 = math.floor(rect.xMinimum() / TILE) * TILE
        e1 = math.floor((rect.xMaximum() - 1e-6) / TILE) * TILE
        n0 = math.floor(rect.yMinimum() / TILE) * TILE
        n1 = math.floor((rect.yMaximum() - 1e-6) / TILE) * TILE
        te = int(e0)
        candidates = []
        while te <= e1:
            tn = int(n0)
            while tn <= n1:
                candidates.append(sheet_id(te, tn))
                tn += int(TILE)
            te += int(TILE)
        if not candidates:
            raise QgsProcessingException("The extent covers no tile.")
        if len(candidates) > MAX_TILES:
            raise QgsProcessingException(
                f"{len(candidates)} candidate tiles — too large. Use a smaller "
                f"AOI or coarser resolution (limit {MAX_TILES}).")
        feedback.pushInfo(f"{len(candidates)} candidate tile(s) in EPSG:31258; "
                          f"checking which exist…")

        # Download each existing tile to a temp dir first (with retry), then warp
        # the LOCAL files. Streaming ~60 tiles straight into gdal.Warp over ~20 min
        # is unreliable on this server — a dropped read aborts the whole warp
        # (returns None near the end). Local files can't drop mid-warp, and a
        # failed download just retries one tile.
        tmp = tempfile.mkdtemp(prefix="salzburg_dgm_")
        try:
            sources = []
            for i, sid in enumerate(candidates):
                if feedback.isCanceled():
                    return {}
                got = _download(f"{BASE}/{sid}_dgm_rp_1_m.tif",
                                os.path.join(tmp, sid + ".tif"), feedback)
                if got:
                    sources.append(got)
                feedback.setProgress(88.0 * (i + 1) / len(candidates))
            if not sources:
                raise QgsProcessingException(
                    "None of the candidate tiles exist — the extent is outside "
                    "Salzburg's DGM coverage.")
            feedback.pushInfo(f"Downloaded {len(sources)} of {len(candidates)} "
                              f"tile(s); warping → {out_path}")

            rect_out = self.parameterAsExtent(parameters, self.AOI, context, out_crs)
            cut = self._cutline(rect_out, out_crs)
            # srcSRS pinned — the archive's tiles are all EPSG:31258, and stating it
            # avoids an intermittent "source has no SRS" cutline warning.
            ds = gdal.Warp(out_path, sources, options=gdal.WarpOptions(
                format="GTiff", srcSRS=TILE_CRS, dstSRS=out_crs.authid(),
                xRes=res, yRes=res, targetAlignedPixels=True,
                cutlineDSName=cut, cropToCutline=True, resampleAlg="bilinear",
                srcNodata=SRC_NODATA, dstNodata=DST_NODATA,
                creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES",
                                 "BIGTIFF=IF_SAFER"]))
            if ds is None:
                raise QgsProcessingException("gdal.Warp produced no output.")
            ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
            xs, ys = ds.RasterXSize, ds.RasterYSize
            ds = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        feedback.setProgress(100)
        feedback.pushInfo(f"✓ {xs} × {ys} px at {res} m, {out_crs.authid()}, "
                          f"nodata {DST_NODATA:g}")
        return {self.OUTPUT: out_path}

    @staticmethod
    def _cutline(rect, crs):
        path = "/vsimem/salzburg_aoi_cut.gpkg"
        if gdal.VSIStatL(path) is not None:
            gdal.Unlink(path)
        srs = osr.SpatialReference(); srs.SetFromUserInput(crs.authid())
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
        lyr = ds.CreateLayer("aoi", srs, ogr.wkbPolygon)
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in [(rect.xMinimum(), rect.yMinimum()),
                     (rect.xMaximum(), rect.yMinimum()),
                     (rect.xMaximum(), rect.yMaximum()),
                     (rect.xMinimum(), rect.yMaximum()),
                     (rect.xMinimum(), rect.yMinimum())]:
            ring.AddPoint(x, y)
        poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(poly); lyr.CreateFeature(f)
        f = lyr = ds = None
        return path
