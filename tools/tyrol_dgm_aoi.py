# -*- coding: utf-8 -*-
"""
Tyrol ALS DGM/DOM AOI → DTM GeoTIFF — a QGIS Processing algorithm.

Builds a terrain (DGM) or surface (DOM) GeoTIFF for a chosen extent from Tyrol's
open ALS/LiDAR tiles (tiris), without a manual tile download. It queries the
tiris tile index (an ArcGIS FeatureServer of tile footprints + download URLs),
reads the DGM/DOM GeoTIFF *inside* each remote ZIP via /vsizip//vsicurl/, and
warps them to your chosen CRS and resolution, cropped to the AOI.

The native tiles are 0.5 m, Float32, nodata -9999, in Austria GK (EPSG:31254 West
/ 31255 Central) — this reprojects/mosaics them into one output. Data © Land
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
    QgsProcessingParameterVectorLayer, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsProject, QgsVectorLayer,
)

try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
except ImportError:      # pragma: no cover
    gdal = None

TILE_INDEX = ("https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/"
              "services/als_tif_3857/FeatureServer/0")
NODATA = -9999.0
METERS_PER_DEGREE = 111319.49079327358
MODELS = [("DGM — terrain (bare earth)", "dgm"),
          ("DOM — surface (incl. buildings/trees)", "dom")]


def _active_layer_id():
    """The active vector layer's id, to pre-fill the AOI. iface is absent
    headless (Processing model / batch), where there is no default."""
    try:
        from qgis.utils import iface
        active = iface.activeLayer() if iface is not None else None
        return active.id() if isinstance(active, QgsVectorLayer) else None
    except Exception:      # pragma: no cover  (no GUI / iface)
        return None


def _aoi_rect(alg, parameters, context, target_crs):
    """The AOI layer's extent in target_crs — the replacement for
    parameterAsExtent now that the AOI is a layer picker."""
    layer = alg.parameterAsVectorLayer(parameters, alg.AOI, context)
    if layer is None:
        raise QgsProcessingException("Choose a vector layer for the area of interest.")
    rect = layer.extent()
    src = layer.crs()
    if src.isValid() and target_crs.isValid() and src != target_crs:
        rect = QgsCoordinateTransform(
            src, target_crs, QgsProject.instance()).transformBoundingBox(rect)
    return rect


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
        return "Tyrol ALS DGM/DOM → DTM GeoTIFF (AOI) (0.5m 31254/31255)"

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
            "(EPSG:31254/31255); pick a coarser resolution (e.g. 1–2 m) for a "
            "large area. Data © Land Tirol, CC BY 4.0."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.AOI, "Area of interest (vector layer, any CRS)",
            defaultValue=_active_layer_id()))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODEL, "Model", options=[m[0] for m in MODELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.RES, "Output resolution (m)", type=QgsProcessingParameterNumber.Double,
            defaultValue=0.5, minValue=0.5, maxValue=50.0))
        self.addParameter(QgsProcessingParameterCrs(
            # Default to the tiles' own grid — reprojecting resamples. Tyrol
            # spans two GK zones; West (31254) covers most of it, and an AOI in
            # Central is still reprojected/mosaicked correctly from here.
            self.CRS, "Output CRS", defaultValue="EPSG:31254"))
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

        rect3857 = _aoi_rect(self, parameters, context, QgsCoordinateReferenceSystem("EPSG:3857"))
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

            rect = _aoi_rect(self, parameters, context, out_crs)
            cut = self._cutline(rect, out_crs)
            warp_res = self._warp_res(res, out_crs, feedback)
            ds = gdal.Warp(out_path, sources, options=gdal.WarpOptions(
                format="GTiff", dstSRS=out_crs.authid(),
                xRes=warp_res, yRes=warp_res, targetAlignedPixels=True,
                cutlineDSName=cut, cropToCutline=True,
                resampleAlg="bilinear", srcNodata=NODATA, dstNodata=NODATA,
                creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES",
                                 "BIGTIFF=IF_SAFER"]))
            if ds is None:
                raise QgsProcessingException("gdal.Warp produced no output.")
            xs, ys = ds.RasterXSize, ds.RasterYSize
            if xs < 2 or ys < 2:
                ds = None
                raise QgsProcessingException(
                    f"The warp produced a degenerate {xs} × {ys} px raster — the "
                    f"resolution is too coarse for the extent in {out_crs.authid()}.")
            ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
            ds = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        feedback.setProgress(100)
        feedback.pushInfo(f"✓ {xs} × {ys} px at {res} m, {out_crs.authid()}, "
                          f"nodata {NODATA:g}")
        return {self.OUTPUT: out_path}

    @staticmethod
    def _warp_res(res_m, out_crs, feedback):
        """The Resolution parameter is metres, but gdal.Warp reads xRes/yRes in
        the OUTPUT CRS's units. Pick a geographic CRS (EPSG:4326) and a 1 m
        request silently becomes 1 DEGREE per pixel — a 31 km AOI warps to a
        1 x 1 px raster reported as a success. Convert, using a single equatorial
        factor so pixels stay square in degrees (as the plugin's
        engine.grid_step_units does)."""
        if not out_crs.isGeographic():
            return res_m
        deg = res_m / METERS_PER_DEGREE
        feedback.pushInfo(
            f"Output CRS {out_crs.authid()} is geographic: {res_m:g} m -> "
            f"{deg:.8f} deg per pixel.")
        return deg

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
