# -*- coding: utf-8 -*-
"""
swisstopo STAC → COG VRT — a QGIS Processing algorithm.

Builds a GDAL VRT over the swisstopo cloud-optimized GeoTIFFs (swissALTI3D DTM,
SWISSIMAGE orthophoto, …) covering a chosen extent, WITHOUT downloading them: the
VRT references the remote COGs through /vsicurl/, so GDAL and QGIS stream only the
pixels they read. Load the output, then export your exact AOI with the Basemap
Tile Downloader's GeoTIFF (local raster) backend.

Drop this file in your profile's processing/scripts/ folder (or Toolbox ▸ Scripts
▸ Add Script to Toolbox) and it appears under Scripts ▸ swisstopo.

swisstopo publishes these as open government data (© swisstopo).
"""
import json
import re
import urllib.parse
import urllib.request

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingContext,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterString, QgsProcessingParameterFileDestination,
    QgsProcessingParameterDefinition, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsProject, QgsVectorLayer,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
except ImportError:      # pragma: no cover - QGIS always ships GDAL
    gdal = None

STAC_API = "https://data.geo.admin.ch/api/stac/v0.9"

# label, collection id, resolution token (as in the filename), pixel size (m),
# nodata (None for RGB imagery, which uses no nodata).
PRESETS = [
    ("swissALTI3D — DTM, 0.5 m", "ch.swisstopo.swissalti3d", "0.5", 0.5, -9999.0),
    ("swissALTI3D — DTM, 2 m", "ch.swisstopo.swissalti3d", "2", 2.0, -9999.0),
    ("SWISSIMAGE — orthophoto, 0.1 m", "ch.swisstopo.swissimage-dop10", "0.1", 0.1, None),
    ("SWISSIMAGE — orthophoto, 2 m", "ch.swisstopo.swissimage-dop10", "2", 2.0, None),
]


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


class SwisstopoStacVrtAlgorithm(QgsProcessingAlgorithm):
    AOI = "AOI"
    PRESET = "PRESET"
    YEAR = "YEAR"
    ALL_YEARS = "ALL_YEARS"
    COLLECTION = "COLLECTION"
    RES = "RES"
    NODATA = "NODATA"
    OUTPUT = "OUTPUT"

    # ── boilerplate ───────────────────────────────────────────────────────────
    def createInstance(self):
        return SwisstopoStacVrtAlgorithm()

    def name(self):
        return "swisstopo_stac_cog_vrt"

    def displayName(self):
        return "swisstopo STAC → COG VRT"

    def group(self):
        return "swisstopo"

    def groupId(self):
        return "swisstopo"

    def shortHelpString(self):
        return (
            "Build a virtual raster (VRT) over swisstopo cloud-optimized GeoTIFFs "
            "covering the chosen extent — without downloading them. The VRT points "
            "at the remote tiles via /vsicurl/, so it loads instantly and streams "
            "only what you view or export.\n\n"
            "Pick a preset (swissALTI3D DTM, or SWISSIMAGE orthophoto), choose "
            "the AOI layer, and run. Load the resulting VRT, then export your "
            "exact AOI with the Basemap Tile Downloader's GeoTIFF backend.\n\n"
            "Where a tile was flown in several years, the newest is kept per cell "
            "unless you set a specific Year or tick 'Keep all years'. The Advanced "
            "fields override the preset with any other swisstopo COG collection "
            "(Collection id + resolution token, e.g. ch.swisstopo.swissaltiregio)."
        )

    # ── parameters ────────────────────────────────────────────────────────────
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.AOI, "Area of interest (vector layer, any CRS)",
            defaultValue=_active_layer_id()))

        self.addParameter(QgsProcessingParameterEnum(
            self.PRESET, "Product", options=[p[0] for p in PRESETS],
            defaultValue=0))

        self.addParameter(QgsProcessingParameterNumber(
            self.YEAR, "Only tiles from this year (optional)",
            type=QgsProcessingParameterNumber.Integer, optional=True,
            minValue=1900, maxValue=2100))

        self.addParameter(QgsProcessingParameterBoolean(
            self.ALL_YEARS, "Keep all years (default: newest per cell)",
            defaultValue=False))

        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "Output VRT", fileFilter="VRT (*.vrt)"))

        # Advanced: override the preset with an arbitrary swisstopo COG collection.
        adv = [
            QgsProcessingParameterString(
                self.COLLECTION,
                "Collection id override (e.g. ch.swisstopo.swissaltiregio)",
                optional=True),
            QgsProcessingParameterString(
                self.RES, "Resolution token override (e.g. 0.5, 2, 0.1)",
                optional=True),
            QgsProcessingParameterNumber(
                self.NODATA, "Nodata override (leave empty for RGB imagery)",
                type=QgsProcessingParameterNumber.Double, optional=True),
        ]
        for p in adv:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(p)

    # ── run ───────────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None:
            raise QgsProcessingException("GDAL Python bindings are unavailable.")

        label, collection, res_token, res_value, nodata = PRESETS[
            self.parameterAsEnum(parameters, self.PRESET, context)]

        # Advanced overrides win, for any other swisstopo COG collection.
        coll_ovr = (self.parameterAsString(parameters, self.COLLECTION, context)
                    or "").strip()
        if coll_ovr:
            res_ovr = (self.parameterAsString(parameters, self.RES, context)
                       or "").strip()
            if not res_ovr:
                raise QgsProcessingException(
                    "A Resolution token override is required when you set a "
                    "Collection id override.")
            collection, res_token = coll_ovr, res_ovr
            try:
                res_value = float(res_token)
            except ValueError:
                raise QgsProcessingException(
                    f"Resolution token '{res_token}' is not a number.")
            nodata = None
            if self.NODATA in parameters and parameters[self.NODATA] is not None:
                nodata = self.parameterAsDouble(parameters, self.NODATA, context)
            label = f"{collection} @ {res_token} m"

        year = None
        if self.YEAR in parameters and parameters[self.YEAR] is not None:
            year = self.parameterAsInt(parameters, self.YEAR, context)
        keep_all = self.parameterAsBool(parameters, self.ALL_YEARS, context)

        # The extent, reprojected to WGS84 lon/lat for the STAC bbox query. QGIS
        # does the reprojection, so the AOI can be given in any CRS.
        rect = _aoi_rect(self, parameters, context, QgsCoordinateReferenceSystem("EPSG:4326"))
        if rect.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")
        bbox_ll = (rect.xMinimum(), rect.yMinimum(),
                   rect.xMaximum(), rect.yMaximum())
        feedback.pushInfo(
            f"{label}\nSTAC bbox (WGS84 lon/lat): "
            + ", ".join(f"{v:.5f}" for v in bbox_ll))

        features = self._stac_items(collection, bbox_ll, feedback)
        if feedback.isCanceled():
            return {}
        feedback.pushInfo(f"STAC returned {len(features)} item(s).")

        hrefs = self._pick_hrefs(features, res_token, year, keep_all, feedback)
        if not hrefs:
            raise QgsProcessingException(
                "No matching COG tiles for that extent / product / year.")
        feedback.pushInfo(f"Selected {len(hrefs)} tile(s) at {res_token} m.")

        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        self._build_vrt(hrefs, out_path, res_value, nodata, feedback)

        # Add the VRT to the project on completion, so it's ready to feed the
        # Basemap Tile Downloader.
        context.addLayerToLoadOnCompletion(
            out_path,
            QgsProcessingContext.LayerDetails(
                "swisstopo DTM/ortho (VRT)", context.project(), self.OUTPUT))
        return {self.OUTPUT: out_path}

    # ── STAC + VRT helpers ────────────────────────────────────────────────────
    @staticmethod
    def _stac_items(collection, bbox_ll, feedback):
        url = (f"{STAC_API}/collections/{collection}/items?"
               + urllib.parse.urlencode(
                   {"bbox": ",".join(map(str, bbox_ll)), "limit": 100}))
        feats, pages = [], 0
        while url and not feedback.isCanceled():
            with urllib.request.urlopen(url, timeout=60) as r:   # nosec B310 (https)
                doc = json.load(r)
            feats.extend(doc.get("features", []))
            pages += 1
            feedback.pushInfo(f"  page {pages}: {len(feats)} item(s) so far…")
            url = next((ln["href"] for ln in doc.get("links", [])
                        if ln.get("rel") == "next"), None)
            if pages > 200:          # safety valve against a pagination loop
                break
        return feats

    @staticmethod
    def _tile_key(item_id):
        parts = item_id.split("_")
        for i, p in enumerate(parts):
            if len(p) == 4 and p.isdigit() and i + 1 < len(parts):
                return int(p), parts[i + 1]
        return None, item_id

    @classmethod
    def _pick_hrefs(cls, features, res_token, year, keep_all, feedback):
        # swissALTI3D: …_<res>_2056_<n>.tif ; SWISSIMAGE: …_<res>_2056.tif — so
        # match "_<res>_2056" followed by '_' or '.', to avoid a loose substring.
        marker = re.compile(re.escape(f"_{res_token}_2056") + r"[._]")
        best, out_all = {}, []
        for f in features:
            yr, tile = cls._tile_key(f.get("id", ""))
            if year is not None and yr != year:
                continue
            href = next((a.get("href") for a in f.get("assets", {}).values()
                         if a.get("href", "").endswith(".tif")
                         and marker.search(a["href"])), None)
            if not href:
                continue
            if keep_all:
                out_all.append(href)
            else:
                cur = best.get(tile)
                if cur is None or (yr or -1) > cur[0]:
                    best[tile] = ((yr or -1), href)
        return out_all if keep_all else [h for _, h in best.values()]

    @staticmethod
    def _build_vrt(hrefs, out_path, res_value, nodata, feedback):
        for k, v in {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                     "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
                     "GDAL_HTTP_MULTIRANGE": "YES",
                     "GDAL_HTTP_VERSION": "2",
                     "VSI_CACHE": "TRUE"}.items():
            gdal.SetConfigOption(k, v)
        sources = ["/vsicurl/" + h for h in hrefs]
        kw = dict(resolution="user", xRes=res_value, yRes=res_value,
                  resampleAlg="nearest")
        if nodata is not None:
            kw.update(srcNodata=nodata, VRTNodata=nodata)
        feedback.pushInfo("Building the VRT (opens each remote COG once)…")
        ds = gdal.BuildVRT(out_path, sources, options=gdal.BuildVRTOptions(**kw))
        if ds is None:
            raise QgsProcessingException("gdal.BuildVRT produced no output.")
        xs, ys, gt = ds.RasterXSize, ds.RasterYSize, ds.GetGeoTransform()
        ds = None
        feedback.pushInfo(
            f"✓ {xs} × {ys} px "
            f"({xs * abs(gt[1]) / 1000:.1f} × {ys * abs(gt[5]) / 1000:.1f} km) "
            f"at {res_value} m, EPSG:2056.")
