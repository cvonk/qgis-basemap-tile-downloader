# -*- coding: utf-8 -*-
"""
Bavaria DGM1 AOI -> VRT — a QGIS Processing algorithm.

Builds a GDAL VRT over the Bavarian open DGM1 (1 m digital terrain model) tiles
covering a chosen extent, WITHOUT downloading them: the VRT references the remote
GeoTIFFs through /vsicurl/, so it loads instantly and streams only the pixels it
reads. Load the output, then export your exact AOI with the Basemap Tile
Downloader's GeoTIFF (local raster) backend.

Bavaria has no open live elevation WMS/WCS (the value service is registration-
gated); the open route is these CC-BY-4.0 tiles. They are 1 km, Float32, nodata
-9999, EPSG:25832 (ETRS89-UTM32), named {E_km}_{N_km}.tif by their south-west
corner. This tool works out which tiles your extent touches, checks which
actually exist (border gaps), and stitches them into one virtual raster.

Data: © Bayerische Vermessungsverwaltung (BVV), CC BY 4.0.

Drop this file in your profile's processing/scripts/ folder (or Toolbox ▸ Scripts
▸ Add Script to Toolbox); it appears under Scripts ▸ Germany (Bayern).
"""
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingContext,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterString,
    QgsProcessingParameterFileDestination, QgsProcessingParameterDefinition,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsProject, QgsRectangle, QgsPointXY, QgsVectorLayer,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
except ImportError:      # pragma: no cover - QGIS always ships GDAL
    gdal = None

TILE_BASE = "https://download1.bayernwolke.de/a/dgm/dgm1"   # mirror: download2
TILE_CRS = "EPSG:25832"
TILE_KM = 1                       # 1 km tiles
RES = 1.0                         # 1 m
NODATA = -9999.0
MAX_TILES = 4000                  # ~4000 km²; a 1 m DGM beyond that is unwieldy


def _active_layer_id():
    """The active vector layer's id, to pre-fill the AOI. iface is absent
    headless (Processing model / batch), where there is no default."""
    try:
        from qgis.utils import iface
        active = iface.activeLayer() if iface is not None else None
        return active.id() if isinstance(active, QgsVectorLayer) else None
    except Exception:      # pragma: no cover  (no GUI / iface)
        return None


_ROT_TOL = 0.002        # >0.2% of the envelope wasted -> the grids are meaningfully rotated
QUERY_MARGIN = 500.0     # m in TILE_CRS, slack so edge pixels have neighbours


def _aoi_layer(alg, parameters, context):
    layer = alg.parameterAsVectorLayer(parameters, alg.AOI, context)
    if layer is None:
        raise QgsProcessingException("Choose a vector layer for the area of interest.")
    return layer


def _aoi_rect(alg, parameters, context, target_crs, margin=0.0):
    """The AOI layer's extent in target_crs, for SELECTING source tiles.

    transformBoundingBox returns the axis-aligned envelope of the reprojected
    rectangle, so a rotated CRS pair inflates it — the harmless direction here, it
    only over-selects tiles. `margin` (in target_crs units) adds slack so resampling
    at the AOI edge always has neighbouring source pixels.
    NOT for the output extent: use _aoi_rect_out, which refuses the rotated case."""
    layer = _aoi_layer(alg, parameters, context)
    rect = layer.extent()
    src = layer.crs()
    if src.isValid() and target_crs.isValid() and src != target_crs:
        rect = QgsCoordinateTransform(
            src, target_crs, QgsProject.instance()).transformBoundingBox(rect)
    if margin:
        rect = QgsRectangle(rect.xMinimum() - margin, rect.yMinimum() - margin,
                            rect.xMaximum() + margin, rect.yMaximum() + margin)
    return rect


def _aoi_rect_out(alg, parameters, context, out_crs):
    """The AOI extent for the CUTLINE / output extent — refuses a rotated reprojection.

    An axis-aligned raster cannot hold an AOI that is rotated relative to it.
    transformBoundingBox would silently pad the canvas out to the envelope, and every
    added pixel is nodata: a 31 km AOI taken from GK Central (31255/31258) into GK West
    (31254) inflates to 33.4 km, ~14% of the output nodata. Pairs that differ by no
    rotation (25832/32632, or a false-easting-only sibling) pass straight through."""
    layer = _aoi_layer(alg, parameters, context)
    rect = layer.extent()
    src = layer.crs()
    if not (src.isValid() and out_crs.isValid()) or src == out_crs:
        return rect
    xf = QgsCoordinateTransform(src, out_crs, QgsProject.instance())
    box = xf.transformBoundingBox(rect)
    p = [xf.transform(QgsPointXY(x, y)) for x, y in
         ((rect.xMinimum(), rect.yMinimum()), (rect.xMaximum(), rect.yMinimum()),
          (rect.xMaximum(), rect.yMaximum()), (rect.xMinimum(), rect.yMaximum()))]
    quad = abs(sum(p[i].x() * p[(i + 1) % 4].y() - p[(i + 1) % 4].x() * p[i].y()
                   for i in range(4))) / 2.0                      # shoelace area
    envelope = box.width() * box.height()
    waste = 1.0 - quad / envelope if envelope > 0 else 0.0
    if waste > _ROT_TOL:
        raise QgsProcessingException(
            f"The AOI layer is in {src.authid()} but the Output CRS is {out_crs.authid()}, "
            f"and those grids are rotated relative to each other. An axis-aligned output "
            f"would be padded out to an envelope that is {waste * 100:.1f}% nodata. "
            f"Reproject the AOI first with Scripts > AOI > 'Reproject / resize AOI' (it "
            f"keeps the centre and writes an exact box), or set Output CRS to "
            f"{src.authid()}.")
    return box


class BavariaDgm1AoiVrt(QgsProcessingAlgorithm):
    AOI = "AOI"
    BASE = "BASE"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return BavariaDgm1AoiVrt()

    def name(self):
        return "bavaria_dgm1_aoi_vrt"

    def displayName(self):
        return "Bavaria DGM1 AOI → VRT (1m 25832)"

    def group(self):
        return "Germany (Bayern)"

    def groupId(self):
        return "germany_bayern"

    def shortHelpString(self):
        return (
            "Build a virtual raster (VRT) over the Bavarian open DGM1 (1 m terrain "
            "model) tiles covering the chosen extent — without downloading them. "
            "The VRT points at the remote tiles via /vsicurl/, so it loads "
            "instantly and streams only what you view or export.\n\n"
            "Pick the AOI layer (any CRS) and run. Load the resulting VRT, "
            "then export your exact AOI with the Basemap Tile Downloader's GeoTIFF "
            "backend.\n\n"
            "Tiles are 1 km, Float32, nodata -9999, EPSG:25832. Data © Bayerische "
            "Vermessungsverwaltung (BVV), CC BY 4.0."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.AOI, "Area of interest (vector layer, any CRS)",
            defaultValue=_active_layer_id()))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "Output VRT", fileFilter="VRT (*.vrt)"))
        # Advanced: point at a mirror, or another bayernwolke product with the same
        # {E_km}_{N_km}.tif / 1 km / EPSG:25832 tiling.
        base = QgsProcessingParameterString(
            self.BASE, "Tile base URL", defaultValue=TILE_BASE)
        base.setFlags(base.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(base)

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None:
            raise QgsProcessingException("GDAL Python bindings are unavailable.")
        base = (self.parameterAsString(parameters, self.BASE, context)
                or TILE_BASE).rstrip("/")

        rect = _aoi_rect(self, parameters, context,
                         QgsCoordinateReferenceSystem(TILE_CRS), margin=QUERY_MARGIN)
        if rect.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")

        # Tiles are keyed by their SW corner in km; a tile (e,n) spans
        # [e*1000, (e+1)*1000) × [n*1000, (n+1)*1000). Collect every km cell the
        # extent touches.
        import math
        e0 = math.floor(rect.xMinimum() / 1000.0)
        e1 = math.floor((rect.xMaximum() - 1e-6) / 1000.0)
        n0 = math.floor(rect.yMinimum() / 1000.0)
        n1 = math.floor((rect.yMaximum() - 1e-6) / 1000.0)
        candidates = [(e, n) for n in range(n0, n1 + 1)
                      for e in range(e0, e1 + 1)]
        if not candidates:
            raise QgsProcessingException("The extent does not cover any tile.")
        if len(candidates) > MAX_TILES:
            raise QgsProcessingException(
                f"{len(candidates)} candidate tiles ({len(candidates)} km²) — too "
                f"much 1 m terrain for one VRT (limit {MAX_TILES}). Use a smaller "
                f"area of interest.")
        feedback.pushInfo(
            f"Extent touches {len(candidates)} km-tile(s) in EPSG:25832 "
            f"(E {e0}–{e1}, N {n0}–{n1}); checking which exist…")

        # vsicurl tuning: don't scan the "directory", cache ranges, use HTTP/2.
        for k, v in {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                     "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
                     "GDAL_HTTP_MULTIRANGE": "YES",
                     "GDAL_HTTP_VERSION": "2",
                     "VSI_CACHE": "TRUE"}.items():
            gdal.SetConfigOption(k, v)

        # Keep only tiles that actually exist (Bavaria's border leaves gaps in the
        # km grid). gdal.Open on a /vsicurl URL reads just the header, and raises
        # for a 404 — that's the existence test.
        sources = []
        total = len(candidates)
        for i, (e, n) in enumerate(candidates):
            if feedback.isCanceled():
                return {}
            url = f"/vsicurl/{base}/{e}_{n}.tif"
            ds = None
            try:
                ds = gdal.Open(url)
            except RuntimeError:
                ds = None                      # 404 / not a raster → skip the gap
            if ds is not None:
                sources.append(url)
                ds = None
            feedback.setProgress(100.0 * (i + 1) / total)

        if not sources:
            raise QgsProcessingException(
                "None of the candidate tiles exist — the extent is outside "
                "Bavaria's DGM1 coverage.")
        feedback.pushInfo(f"{len(sources)} of {total} tile(s) exist; building VRT…")

        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        opts = gdal.BuildVRTOptions(resolution="user", xRes=RES, yRes=RES,
                                    srcNodata=NODATA, VRTNodata=NODATA,
                                    resampleAlg="nearest")
        ds = gdal.BuildVRT(out_path, sources, options=opts)
        if ds is None:
            raise QgsProcessingException("gdal.BuildVRT produced no output.")
        xs, ys, gt = ds.RasterXSize, ds.RasterYSize, ds.GetGeoTransform()
        ds = None
        feedback.pushInfo(
            f"✓ {xs} × {ys} px "
            f"({xs * abs(gt[1]) / 1000:.1f} × {ys * abs(gt[5]) / 1000:.1f} km) "
            f"at {RES} m, EPSG:25832, nodata {NODATA:g}.")

        context.addLayerToLoadOnCompletion(
            out_path,
            QgsProcessingContext.LayerDetails(
                "Bavaria DGM1 (VRT)", context.project(), self.OUTPUT))
        return {self.OUTPUT: out_path}
