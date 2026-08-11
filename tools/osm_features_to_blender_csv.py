# -*- coding: utf-8 -*-
"""
OSM features → draped CSV for Blender — a QGIS Processing algorithm.

Pulls named map features from OpenStreetMap over an AOI, samples the DTM so each
one carries a real elevation, builds a label, and writes a single CSV whose rows
are tagged with a `category` column — so a Blender importer can drop each feature
type into its own collection.

Replaces the by-hand QuickOSM → reproject → Field Calculator → Drape → Filter →
Export chain: tick the types you want and run it once.

Feature types and their OSM tags:

  Mountain peaks   natural=peak
  Towns            place = city | town | village | hamlet
  Huts             tourism = alpine_hut | wilderness_hut | chalet
  Valleys          natural=valley
  Rivers           waterway=river

Everything is requested with Overpass `out tags center`, so ways and relations
(valleys, rivers, some huts) come back as a single representative point — which
is all a label needs, and avoids reconstructing geometry.

Only features with a name are kept. The minimum-elevation filter applies to
PEAKS ONLY: towns and huts sit far below any sensible summit threshold, so
applying it to them would empty those categories.

Needs GDAL + an internet connection. Drop in a profile's processing/scripts/
folder; appears under Scripts ▸ Blender.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsPoint, QgsPointXY, QgsProcessingAlgorithm,
    QgsProcessingException, QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
    QgsProject, QgsVectorFileWriter, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "QGIS osm_features_to_blender_csv (hiking-video terrain pipeline)"
LABEL_LEN = 40                    # matches the HOWTO's "Output field length = 40"

# key -> (label, Overpass selector, elevation-filtered)
FEATURES = [
    ("PEAKS",   "Mountain peaks",  'node["natural"="peak"]', True),
    ("TOWNS",   "Towns",           'node["place"~"^(city|town|village|hamlet)$"]', False),
    ("HUTS",    "Huts",            'nwr["tourism"~"^(alpine_hut|wilderness_hut|chalet)$"]', False),
    ("VALLEYS", "Valleys",         'nwr["natural"="valley"]', False),
    ("RIVERS",  "Rivers",          'way["waterway"="river"]', False),
]


def _active_layer_id():
    try:
        from qgis.utils import iface
        active = iface.activeLayer() if iface is not None else None
        return active.id() if isinstance(active, QgsVectorLayer) else None
    except Exception:      # pragma: no cover  (no GUI / iface)
        return None


class OsmFeaturesToBlenderCsv(QgsProcessingAlgorithm):
    AOI = "AOI"
    DTM = "DTM"
    MIN_ELE = "MIN_ELE"
    Z_OFFSET = "Z_OFFSET"
    NODATA = "NODATA"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return OsmFeaturesToBlenderCsv()

    def name(self):
        return "osm_features_to_blender_csv"

    def displayName(self):
        return "OSM features → draped CSV for Blender"

    def group(self):
        return "Blender"

    def groupId(self):
        return "blender"

    def shortHelpString(self):
        return (
            "Fetch named OSM features over an AOI, drape them on a DTM, and write "
            "one CSV for Blender.\n\n"
            "Tick the feature types you want. Each row carries a <b>category</b> "
            "column (peaks / towns / huts / valleys / rivers), so a Blender "
            "importer can build one collection per type from a single file.\n\n"
            "<b>Minimum elevation</b> applies to <b>peaks only</b> — towns and huts "
            "sit well below any summit threshold, so filtering them the same way "
            "would empty those categories. Peaks with no <tt>ele</tt> tag are "
            "dropped by the filter, as in the manual recipe.\n\n"
            "<b>Z offset</b> lifts the point above the terrain so a label floats "
            "clear of the summit rather than sitting in it.\n\n"
            "Output columns: X, Y, Z, category, osm_id, label, ele_m — written "
            "with GEOMETRY=AS_XYZ and a .csvt sidecar, in the DTM's CRS."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.AOI, "Area of interest (vector layer, any CRS)",
            defaultValue=_active_layer_id()))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DTM, "DTM to drape onto"))
        for key, label, _sel, _filt in FEATURES:
            self.addParameter(QgsProcessingParameterBoolean(
                key, label, defaultValue=(key == "PEAKS")))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_ELE, "Minimum elevation for peaks (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=2500.0, minValue=0.0, maxValue=9000.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.Z_OFFSET, "Z offset above terrain (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=50.0, minValue=-1000.0, maxValue=1000.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.NODATA, "Elevation to use where the DTM has no data (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=500.0, minValue=-500.0, maxValue=9000.0))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "Output CSV", fileFilter="CSV (*.csv)"))

    # ── Overpass ────────────────────────────────────────────────────────────
    @staticmethod
    def _query(selector, bbox, feedback):
        """bbox is (south, west, north, east) in WGS84."""
        q = ("[out:json][timeout:180];\n(%s(%.6f,%.6f,%.6f,%.6f););\nout tags center;"
             % ((selector,) + bbox))
        data = urllib.parse.urlencode({"data": q}).encode("utf-8")
        req = urllib.request.Request(OVERPASS, data=data,
                                     headers={"User-Agent": USER_AGENT})
        for attempt in range(3):
            if feedback.isCanceled():
                return []
            try:
                with urllib.request.urlopen(req, timeout=200) as r:   # nosec B310
                    body = r.read()
                return json.loads(body.decode("utf-8")).get("elements", [])
            except (urllib.error.URLError, ValueError) as e:
                # Overpass answers an HTML error page when it is rate-limiting;
                # json.loads then fails. Back off and try again.
                feedback.pushInfo("   Overpass attempt %d failed (%s); retrying…"
                                  % (attempt + 1, type(e).__name__))
                if attempt == 2:
                    raise QgsProcessingException(
                        "Overpass query failed after 3 attempts: %s" % e)
                import time
                time.sleep(20 * (attempt + 1))
        return []

    @staticmethod
    def _to_float(raw):
        """OSM ele can be '3025', '3025 m', '3025.5' or nonsense."""
        if raw is None:
            return None
        try:
            return float(str(raw).split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return None

    def processAlgorithm(self, parameters, context, feedback):
        aoi = self.parameterAsVectorLayer(parameters, self.AOI, context)
        dtm = self.parameterAsRasterLayer(parameters, self.DTM, context)
        if aoi is None:
            raise QgsProcessingException("Choose a vector layer for the area of interest.")
        if dtm is None:
            raise QgsProcessingException("Choose a DTM raster.")
        min_ele = self.parameterAsDouble(parameters, self.MIN_ELE, context)
        z_off = self.parameterAsDouble(parameters, self.Z_OFFSET, context)
        nodata = self.parameterAsDouble(parameters, self.NODATA, context)
        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        wanted = [(k, lab, sel, filt) for k, lab, sel, filt in FEATURES
                  if self.parameterAsBool(parameters, k, context)]
        if not wanted:
            raise QgsProcessingException("Tick at least one feature type.")

        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        out_crs = dtm.crs()
        tc = QgsProject.instance().transformContext()

        # AOI extent -> WGS84 bbox for Overpass (south, west, north, east)
        rect = aoi.extent()
        if aoi.crs() != wgs:
            rect = QgsCoordinateTransform(aoi.crs(), wgs, tc).transformBoundingBox(rect)
        bbox = (rect.yMinimum(), rect.xMinimum(), rect.yMaximum(), rect.xMaximum())
        feedback.pushInfo("AOI bbox (WGS84): %.5f,%.5f,%.5f,%.5f" % bbox)
        feedback.pushInfo("Draping onto %s (%s)" % (dtm.name(), out_crs.authid()))

        # Overpass takes a lat/lon box. Reprojecting a UTM square to WGS84 bulges
        # its edges, so the query returns features just outside the AOI — which
        # then sample the DTM off its edge and come back as nodata. Keep the AOI's
        # real rectangle in the output CRS and reject anything beyond it.
        keep_rect = aoi.extent()
        if aoi.crs() != out_crs:
            keep_rect = QgsCoordinateTransform(
                aoi.crs(), out_crs, tc).transformBoundingBox(keep_rect)

        to_out = QgsCoordinateTransform(wgs, out_crs, tc)
        provider = dtm.dataProvider()

        layer = QgsVectorLayer("PointZ?crs=" + out_crs.authid(), "features", "memory")
        dp = layer.dataProvider()
        dp.addAttributes([
            QgsField("category", QMetaType.Type.QString, len=20),
            QgsField("osm_id", QMetaType.Type.QString, len=20),
            QgsField("label", QMetaType.Type.QString, len=LABEL_LEN),
            QgsField("ele_m", QMetaType.Type.Double),
        ])
        layer.updateFields()

        feats, no_data_hits, outside = [], 0, 0
        for i, (key, label, selector, ele_filtered) in enumerate(wanted):
            if feedback.isCanceled():
                return {}
            feedback.pushInfo("\n%s — querying Overpass…" % label)
            elements = self._query(selector, bbox, feedback)
            kept = skipped_noname = skipped_low = 0
            for el in elements:
                tags = el.get("tags") or {}
                name = tags.get("name")
                if not name:
                    skipped_noname += 1
                    continue
                ele = self._to_float(tags.get("ele"))
                if ele_filtered and (ele is None or ele < min_ele):
                    skipped_low += 1
                    continue
                if el.get("type") == "node":
                    lon, lat = el.get("lon"), el.get("lat")
                else:
                    c = el.get("center") or {}
                    lon, lat = c.get("lon"), c.get("lat")
                if lon is None or lat is None:
                    continue

                pt = to_out.transform(QgsPointXY(float(lon), float(lat)))
                if not keep_rect.contains(pt):
                    outside += 1
                    continue
                z, ok = provider.sample(pt, 1)
                if not ok or z is None or z <= -9000:
                    z = nodata
                    no_data_hits += 1

                # the HOWTO's label expression, in Python
                text = tags.get("name:de") or name
                if ele is not None:
                    text += " (%d m)" % int(ele)

                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry(QgsPoint(pt.x(), pt.y(), z + z_off)))
                f.setAttributes([key.lower(), str(el.get("id", "")),
                                 text[:LABEL_LEN], ele])
                feats.append(f)
                kept += 1
            feedback.pushInfo("   %d kept  (%d unnamed%s)"
                              % (kept, skipped_noname,
                                 ", %d below %.0f m" % (skipped_low, min_ele)
                                 if ele_filtered else ""))
            feedback.setProgress(85.0 * (i + 1) / len(wanted))

        if not feats:
            raise QgsProcessingException(
                "Nothing matched — is the AOI over an area OSM covers, and are the "
                "elevation filter and feature types set as you expect?")
        dp.addFeatures(feats)
        layer.updateExtents()
        if outside:
            feedback.pushInfo(
                "%d feature(s) dropped as outside the AOI (the WGS84 query box is "
                "slightly larger than the AOI rectangle)." % outside)
        if no_data_hits:
            feedback.pushWarning(
                "%d feature(s) fell on DTM nodata inside the AOI and were given "
                "%.0f m — check for holes in the DTM there." % (no_data_hits, nodata))

        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "CSV"
        opts.fileEncoding = "UTF-8"
        opts.layerOptions = ["CREATE_CSVT=YES", "GEOMETRY=AS_XYZ",
                             "SEPARATOR=COMMA", "STRING_QUOTING=IF_NEEDED"]
        err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, out_path, tc, opts)
        if err[0] != QgsVectorFileWriter.NoError:
            raise QgsProcessingException("Could not write the CSV: %s" % (err[1],))

        feedback.setProgress(100)
        feedback.pushInfo("\n✓ %d features → %s" % (len(feats), out_path))
        return {self.OUTPUT: out_path}
