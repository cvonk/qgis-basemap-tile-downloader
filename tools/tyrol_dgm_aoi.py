# -*- coding: utf-8 -*-
"""
Tyrol ALS DGM/DOM AOI → DTM GeoTIFF — a QGIS Processing algorithm.

Builds a terrain (DGM) or surface (DOM) GeoTIFF for a chosen extent from Tyrol's
open ALS/LiDAR tiles (tiris), without a manual tile download. It queries the
tiris tile index (an ArcGIS FeatureServer of tile footprints + download URLs),
reads the DGM/DOM GeoTIFF *inside* each remote ZIP via /vsizip//vsicurl/, and
warps them to your chosen CRS and resolution, cropped to the AOI.

The native tiles are 0.5 m, Float32, nodata -9999, in Austria GK (EPSG:31254 west
/ 31257 central) — this reprojects/mosaics them into one output. Data © Land
Tirol (tiris), CC BY 4.0.

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸ Austria.
"""
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterExtent, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination, QgsCoordinateReferenceSystem,
)

try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
except ImportError:      # pragma: no cover
    gdal = None

TILE_INDEX = ("https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/"
              "services/als_tif_3857/FeatureServer/0")
NODATA = -9999.0
MODELS = [("DGM — terrain (bare earth)", "dgm"),
          ("DOM — surface (incl. buildings/trees)", "dom")]


class TyrolDgmAoi(QgsProcessingAlgorithm):
    AOI = "AOI"
    MODEL = "MODEL"
    RES = "RES"
    CRS = "CRS"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return TyrolDgmAoi()

    def name(self):
        return "tyrol_dgm_aoi"

    def displayName(self):
        return "Tyrol ALS DGM/DOM → DTM GeoTIFF (AOI)"

    def group(self):
        return "Austria"

    def groupId(self):
        return "austria"

    def shortHelpString(self):
        return (
            "Build a terrain (DGM) or surface (DOM) GeoTIFF for the chosen extent "
            "from Tyrol's open ALS/LiDAR tiles (tiris), without downloading tiles "
            "by hand.\n\nIt queries the tiris tile index, reads the DGM/DOM inside "
            "each remote ZIP via /vsizip//vsicurl/, and warps them to your CRS and "
            "resolution, cropped to the AOI. Native tiles are 0.5 m Float32 "
            "(EPSG:31254/31257); pick a coarser resolution (e.g. 1–2 m) for a "
            "large area. Data © Land Tirol, CC BY 4.0."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(
            self.AOI, "Area of interest (any CRS)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODEL, "Model", options=[m[0] for m in MODELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.RES, "Output resolution (m)", type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=0.5, maxValue=50.0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Output CRS", defaultValue="EPSG:32632"))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, "Output DTM"))

    # ── tile index query ────────────────────────────────────────────────────
    @staticmethod
    def _tiles_for(bbox3857, feedback):
        """(name, zip_url) for every tile whose footprint intersects the 3857 bbox."""
        out, offset = [], 0
        while True:
            q = urllib.parse.urlencode({
                "f": "json", "where": "1=1",
                "geometry": ",".join(f"{v:.3f}" for v in bbox3857),
                "geometryType": "esriGeometryEnvelope", "inSR": "3857",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "NAME,URL", "returnGeometry": "false",
                "resultRecordCount": 2000, "resultOffset": offset})
            with urllib.request.urlopen(f"{TILE_INDEX}/query?{q}",  # nosec B310
                                        timeout=60) as r:
                doc = json.load(r)
            feats = doc.get("features", [])
            for f in feats:
                a = f["attributes"]
                if a.get("URL"):
                    out.append((a.get("NAME"), a["URL"]))
            if len(feats) < 2000 or not doc.get("exceededTransferLimit"):
                break
            offset += len(feats)
            if feedback.isCanceled():
                break
        return out

    @staticmethod
    def _fetch_dgm(url, prefix, dst, feedback, tries=4):
        """Copy the DGM (or DOM) GeoTIFF out of a tile ZIP to the local file `dst`,
        retrying transient failures. Reads only that member (not the DOM/hillshade
        siblings) via /vsizip//vsicurl/, but writes it to disk so the later warp
        runs on local files — streaming every member straight into a long warp
        drops out on this server. Returns dst, or None (no DGM / gave up)."""
        vz = "/vsizip/{/vsicurl/%s}" % url
        for attempt in range(tries):
            if feedback.isCanceled():
                return None
            try:
                member = None
                for e in (gdal.ReadDir(vz) or []):
                    el = e.lower()
                    if (el.startswith(prefix + "_") and "_shd_" not in el
                            and el.endswith(".tif")):
                        member = vz + "/" + e
                        break
                if member is None:
                    return None              # ZIP has no matching raster
                gdal.Translate(dst, member,
                               options=gdal.TranslateOptions(format="GTiff"))
                if gdal.Open(dst) is not None:
                    return dst
            except Exception:      # noqa: BLE001  (transient network error → retry)
                pass
            time.sleep(2 * (attempt + 1))
        return None

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None:
            raise QgsProcessingException("GDAL Python bindings are unavailable.")
        prefix = MODELS[self.parameterAsEnum(parameters, self.MODEL, context)][1]
        res = self.parameterAsDouble(parameters, self.RES, context)
        out_crs = self.parameterAsCrs(parameters, self.CRS, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        rect3857 = self.parameterAsExtent(
            parameters, self.AOI, context, QgsCoordinateReferenceSystem("EPSG:3857"))
        if rect3857.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")
        bbox = (rect3857.xMinimum(), rect3857.yMinimum(),
                rect3857.xMaximum(), rect3857.yMaximum())

        feedback.pushInfo("Querying the tiris tile index…")
        tiles = self._tiles_for(bbox, feedback)
        if not tiles:
            raise QgsProcessingException(
                "No Tyrol ALS tiles intersect the extent (outside Tyrol?).")
        feedback.pushInfo(f"{len(tiles)} tile(s) intersect; locating "
                          f"{prefix.upper()} rasters inside each ZIP…")

        for k, v in {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                     "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".zip,.tif",
                     "GDAL_HTTP_MULTIRANGE": "YES", "GDAL_HTTP_VERSION": "2",
                     "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "2",
                     "VSI_CACHE": "TRUE"}.items():
            gdal.SetConfigOption(k, v)

        # Copy each tile's DGM to a local temp file first (retryable), then warp
        # the LOCAL files. Streaming ~200 remote members into one long warp is
        # unreliable — a dropped read aborts the whole warp near the end. Local
        # files can't drop mid-warp, and a failed copy just retries one tile.
        tmp = tempfile.mkdtemp(prefix="tyrol_dgm_")
        try:
            sources = []
            for i, (name, url) in enumerate(tiles):
                if feedback.isCanceled():
                    return {}
                dst = os.path.join(tmp, f"{name or i}.tif")
                if self._fetch_dgm(url, prefix, dst, feedback):
                    sources.append(dst)
                feedback.setProgress(88.0 * (i + 1) / len(tiles))
            if not sources:
                raise QgsProcessingException(
                    f"Found tiles but no {prefix.upper()} raster inside their ZIPs.")
            feedback.pushInfo(f"Downloaded {len(sources)} tile(s); warping → {out_path}")

            rect = self.parameterAsExtent(parameters, self.AOI, context, out_crs)
            cut = self._cutline(rect, out_crs)
            ds = gdal.Warp(out_path, sources, options=gdal.WarpOptions(
                format="GTiff", dstSRS=out_crs.authid(),
                xRes=res, yRes=res, targetAlignedPixels=True,
                cutlineDSName=cut, cropToCutline=True,
                resampleAlg="bilinear", srcNodata=NODATA, dstNodata=NODATA,
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
                          f"nodata {NODATA:g}")
        return {self.OUTPUT: out_path}

    @staticmethod
    def _cutline(rect, crs):
        path = "/vsimem/tyrol_aoi_cut.gpkg"
        for p in (path,):
            if gdal.VSIStatL(p) is not None:
                gdal.Unlink(p)
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
