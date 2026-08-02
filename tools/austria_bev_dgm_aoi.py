# -*- coding: utf-8 -*-
"""
Austria BEV ALS DGM/DOM AOI → DTM GeoTIFF — a QGIS Processing algorithm.

Builds a terrain (DGM) or surface (DOM) GeoTIFF for a chosen extent from BEV's
nationwide open ALS/LiDAR 1 m height rasters — so it covers all of Austria,
including provinces with no open per-tile service of their own (e.g. Upper
Austria / Oberösterreich).

BEV publishes the data as Cloud-Optimized GeoTIFF (COG) tiles of 50 km on the
ETRS89-LAEA grid (EPSG:3035), 1 m, Float32, nodata -9999. Because they are COGs,
this reads only the AOI window over /vsicurl/ — it never downloads a whole 50 km
tile — and warps that window to your CRS and resolution, cropped to the AOI. Each
tile carries a yearly reference date (Stichtag, 15 Sep) in its path that differs
by region and update year, so the newest existing date is probed per tile. Data
© BEV (Bundesamt für Eich- und Vermessungswesen), CC BY 4.0.

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸ Austria.
"""
import datetime
import math

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

BASE = "https://data.bev.gv.at/download/ALS"
GRID_CRS = "EPSG:3035"             # the tiles' native ETRS89-LAEA grid
TILE = 50000                       # tile side, metres
NODATA = -9999.0
MAX_TILES = 40                     # a 16.5 km AOI touches ≤ 4; guard a runaway AOI
METERS_PER_DEGREE = 111319.49079327358
MODELS = [("DGM — terrain (bare earth)", "DTM"),
          ("DOM — surface (incl. buildings/trees)", "DSM")]


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


class AustriaBevDgmAoi(QgsProcessingAlgorithm):
    AOI = "AOI"
    MODEL = "MODEL"
    RES = "RES"
    CRS = "CRS"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return AustriaBevDgmAoi()

    def name(self):
        return "austria_bev_dgm_aoi"

    def displayName(self):
        # The trailing (native resolution, native CRS) is what you are actually
        # sampling — the Output CRS / Resolution parameters only say what it is
        # resampled to, so the source grid belongs in the name.
        return "Austria BEV ALS DGM/DOM → DTM GeoTIFF (AOI) (1m 3035)"

    def group(self):
        return "Austria"

    def groupId(self):
        return "austria"

    def shortHelpString(self):
        return (
            "Build a terrain (DGM) or surface (DOM) GeoTIFF for the chosen extent "
            "from BEV's nationwide open ALS 1 m height rasters — covers all of "
            "Austria, including regions without their own open tile service (e.g. "
            "Upper Austria).\n\nThe data is Cloud-Optimized GeoTIFF on a 50 km "
            "EPSG:3035 grid (1 m, Float32), so only the AOI window is read over "
            "/vsicurl/ (no 50 km tile download) and warped to your CRS and "
            "resolution, cropped to the AOI. Tiles off Austria simply don't exist "
            "and are skipped. Data © BEV, CC BY 4.0."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            # A layer picker rather than an extent box, so the Toolbox shows the
            # AOI layer's NAME instead of four coordinates (as the plugin dialog
            # does). Defaults to the active layer when there is a GUI.
            self.AOI, "Area of interest (vector layer, any CRS)",
            defaultValue=_active_layer_id()))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODEL, "Model", options=[m[0] for m in MODELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.RES, "Output resolution (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0, minValue=1.0, maxValue=50.0))
        self.addParameter(QgsProcessingParameterCrs(
            # Default to the tiles' own grid: reprojecting resamples, and this
            # is usually an intermediate that gets merged or exported later.
            self.CRS, "Output CRS", defaultValue=GRID_CRS))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, "Output DTM"))

    @staticmethod
    def _dates():
        """Candidate Stichtag dates, newest first: 15 Sep of each year from the
        current year back to 2020. Releases are annual, so this auto-extends as
        new years appear without a code change."""
        y = datetime.date.today().year
        return [f"{yr}0915" for yr in range(y, 2019, -1)]

    def _find_tile(self, kind, n0, e0, feedback, prefer=None):
        """/vsicurl path for the newest existing Stichtag of the (n0, e0) tile, or
        None if the tile doesn't exist (outside coverage). `prefer` (a date that
        worked for an earlier tile) is tried first — neighbouring tiles almost
        always share a release date, so this skips most probes."""
        name = f"ALS_{kind}_CRS3035RES50000mN{n0}E{e0}.tif"
        dates = self._dates()
        if prefer in dates:
            dates = [prefer] + [d for d in dates if d != prefer]
        for d in dates:
            if feedback.isCanceled():
                return None
            url = f"{BASE}/{kind}/{d}/{name}"
            try:
                if gdal.Open("/vsicurl/" + url) is not None:
                    return "/vsicurl/" + url, d
            except Exception:      # noqa: BLE001  (missing tile/date → try the next)
                pass
        return None

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None:
            raise QgsProcessingException("GDAL Python bindings are unavailable.")
        kind = MODELS[self.parameterAsEnum(parameters, self.MODEL, context)][1]
        res = self.parameterAsDouble(parameters, self.RES, context)
        out_crs = self.parameterAsCrs(parameters, self.CRS, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        rect = _aoi_rect(self, parameters, context,
                         QgsCoordinateReferenceSystem(GRID_CRS))
        if rect.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")

        e0 = math.floor(rect.xMinimum() / TILE) * TILE
        e1 = math.floor((rect.xMaximum() - 1e-6) / TILE) * TILE
        n0 = math.floor(rect.yMinimum() / TILE) * TILE
        n1 = math.floor((rect.yMaximum() - 1e-6) / TILE) * TILE
        candidates = []
        ee = int(e0)
        while ee <= e1:
            nn = int(n0)
            while nn <= n1:
                candidates.append((nn, ee))
                nn += int(TILE)
            ee += int(TILE)
        if len(candidates) > MAX_TILES:
            raise QgsProcessingException(
                f"{len(candidates)} candidate tiles — too large. Use a smaller AOI "
                f"or coarser resolution (limit {MAX_TILES}).")

        for k, v in {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                     "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
                     "GDAL_HTTP_MULTIRANGE": "YES", "GDAL_HTTP_VERSION": "2",
                     "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "2",
                     "VSI_CACHE": "TRUE"}.items():
            gdal.SetConfigOption(k, v)

        feedback.pushInfo(f"{len(candidates)} candidate tile(s) in EPSG:3035; "
                          f"locating the newest {kind} COG for each…")
        sources, prefer = [], None
        for i, (nn, ee) in enumerate(candidates):
            if feedback.isCanceled():
                return {}
            found = self._find_tile(kind, nn, ee, feedback, prefer)
            if found:
                url, prefer = found            # reuse this date for the next tiles
                sources.append(url)
            feedback.setProgress(15.0 * (i + 1) / len(candidates))
        if not sources:
            raise QgsProcessingException(
                "No BEV ALS tiles cover the extent (outside Austria?).")
        feedback.pushInfo(f"{len(sources)} tile(s) cover the AOI; warping the "
                          f"window → {out_path}")

        rect_out = _aoi_rect(self, parameters, context, out_crs)
        cut = self._cutline(rect_out, out_crs)

        # The warp reads only the AOI window from the COGs and is the slow step;
        # drive the task bar over its 15→100% tail. Always returns 1 (never aborts).
        def _progress(fraction, _msg=None, _data=None):
            feedback.setProgress(15.0 + fraction * 85.0)
            return 1

        # srcSRS pinned — the COGs embed the grid as a LOCAL_CS wrapper that GDAL
        # won't reproject from; stating EPSG:3035 fixes that (and silences a
        # "source has no SRS" cutline warning).
        warp_res = self._warp_res(res, out_crs, feedback)
        ds = gdal.Warp(out_path, sources, options=gdal.WarpOptions(
            format="GTiff", srcSRS=GRID_CRS, dstSRS=out_crs.authid(),
            xRes=warp_res, yRes=warp_res, targetAlignedPixels=True,
            cutlineDSName=cut, cropToCutline=True, resampleAlg="bilinear",
            srcNodata=NODATA, dstNodata=NODATA, multithread=True, callback=_progress,
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
        feedback.setProgress(100)
        feedback.pushInfo(f"✓ {xs} × {ys} px at {res} m, {out_crs.authid()}, "
                          f"nodata {NODATA:g}")
        return {self.OUTPUT: out_path}

    @staticmethod
    def _warp_res(res_m, out_crs, feedback):
        """The Resolution parameter is metres, but gdal.Warp reads xRes/yRes in
        the OUTPUT CRS's units. Pick a geographic CRS (EPSG:4326) and a 1 m
        request silently became 1 DEGREE per pixel — a 31 km AOI warped to a
        1 × 1 px raster that the algorithm then reported as a success. Convert,
        using a single equatorial factor so the pixels stay square in degrees
        (as engine.grid_step_units does in the plugin)."""
        if not out_crs.isGeographic():
            return res_m
        deg = res_m / METERS_PER_DEGREE
        feedback.pushInfo(
            f"Output CRS {out_crs.authid()} is geographic: {res_m:g} m → "
            f"{deg:.8f}° per pixel.")
        return deg

    @staticmethod
    def _cutline(rect, crs):
        path = "/vsimem/austria_bev_aoi_cut.gpkg"
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
