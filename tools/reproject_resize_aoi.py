# -*- coding: utf-8 -*-
"""
Reproject & resize an AOI — a QGIS Processing algorithm.

Takes an area-of-interest vector layer (any CRS), reprojects its *centre* to a
target CRS, and builds a fresh axis-aligned rectangle of a fixed size (default
15500 m) around that centre — so the AOI keeps its location but becomes a clean,
straight box in the target CRS's grid (no rotation from meridian convergence, no
odd fractional extent). The output is a single-polygon layer you can feed straight
into the DGM/ortho tools or the plugin as your export extent.

Centre is preserved by transforming the source layer extent's centre point to the
target CRS (not by reprojecting the whole rectangle and taking its bbox), so it
stays put even when the two CRSes are rotated relative to each other.

The AOI picker lists only vector layers and defaults to the active layer, so with
an AOI layer selected in the Layers panel it is a one-click run.

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸ Area of
interest.
"""
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterNumber,
    QgsProcessingParameterCrs, QgsProcessingParameterFeatureSink,
    QgsCoordinateTransform, QgsProject, QgsVectorLayer,
    QgsFeature, QgsFields, QgsField, QgsGeometry, QgsRectangle, QgsPointXY,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


class ReprojectResizeAoi(QgsProcessingAlgorithm):
    AOI = "AOI"
    CRS = "CRS"
    WIDTH = "WIDTH"
    HEIGHT = "HEIGHT"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return ReprojectResizeAoi()

    def name(self):
        return "reproject_resize_aoi"

    def displayName(self):
        return "Reproject & resize AOI (fixed size, aligned to target CRS)"

    def group(self):
        return "Area of interest"

    def groupId(self):
        return "aoi"

    def shortHelpString(self):
        return (
            "Take an AOI in any CRS, reproject its centre to a target CRS, and "
            "build a fresh axis-aligned rectangle of a fixed size around that "
            "centre.\n\nThe centre is preserved exactly; the box is straight in "
            "the target CRS's grid (no rotation, no fractional extent). Set width "
            "and height in metres (both default 15500 m — set them equal for a "
            "square). Output is a one-polygon layer usable directly as an export "
            "extent for the DGM/ortho tools or the plugin.\n\nThe AOI is a vector "
            "layer (its extent's centre is used) and defaults to the active layer. "
            "Width/height are interpreted in the target CRS's units — use a "
            "projected CRS (metres), not a geographic one (degrees)."
        )

    def initAlgorithm(self, config=None):
        # Default the AOI to the active layer when it's a vector layer, so with an
        # AOI selected in the Layers panel this runs in one click. iface is absent
        # headless (Processing model / batch) — fall back to no default there.
        default_aoi = None
        try:
            from qgis.utils import iface
            active = iface.activeLayer() if iface is not None else None
            if isinstance(active, QgsVectorLayer):
                default_aoi = active.id()
        except Exception:      # pragma: no cover  (no GUI / iface)
            default_aoi = None
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.AOI, "Area of interest (vector layer)", defaultValue=default_aoi))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Target CRS", defaultValue="EPSG:32632"))
        self.addParameter(QgsProcessingParameterNumber(
            self.WIDTH, "Width — West↔East (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=15500.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.HEIGHT, "Height — North↔South (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=15500.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Reprojected AOI",
            type=QgsProcessing.TypeVectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):
        out_crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not out_crs.isValid():
            raise QgsProcessingException("The target CRS is invalid.")
        if out_crs.isGeographic():
            feedback.pushWarning(
                "Target CRS is geographic (degrees) — width/height are in degrees, "
                "not metres. Pick a projected CRS for a metric box.")
        width = self.parameterAsDouble(parameters, self.WIDTH, context)
        height = self.parameterAsDouble(parameters, self.HEIGHT, context)

        layer = self.parameterAsVectorLayer(parameters, self.AOI, context)
        if layer is None:
            raise QgsProcessingException("Choose a vector layer for the AOI.")
        src_crs = layer.crs()
        src_rect = layer.extent()                # in the layer's own CRS
        if src_rect.isEmpty():
            raise QgsProcessingException("The area-of-interest layer has an empty extent.")

        # Preserve the centre exactly: transform the source centre point (not the
        # whole rectangle's bbox) into the target CRS.
        centre = QgsPointXY(src_rect.center())
        if src_crs.isValid() and src_crs != out_crs:
            tr = QgsCoordinateTransform(src_crs, out_crs, QgsProject.instance())
            centre = tr.transform(centre)

        hw, hh = width / 2.0, height / 2.0
        rect = QgsRectangle(centre.x() - hw, centre.y() - hh,
                            centre.x() + hw, centre.y() + hh)

        fields = QgsFields()
        fields.append(QgsField("width_m", QVariant.Double))
        fields.append(QgsField("height_m", QVariant.Double))
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields,
            QgsWkbTypes.Polygon, out_crs)
        if sink is None:
            raise QgsProcessingException("Could not create the output layer.")
        feat = QgsFeature(fields)
        feat.setGeometry(QgsGeometry.fromRect(rect))
        feat.setAttributes([width, height])
        sink.addFeature(feat)

        feedback.pushInfo(
            f"Centre {centre.x():.3f}, {centre.y():.3f} in {out_crs.authid()}")
        feedback.pushInfo(
            f"Extent {rect.xMinimum():.3f},{rect.xMaximum():.3f},"
            f"{rect.yMinimum():.3f},{rect.yMaximum():.3f} [{out_crs.authid()}]  "
            f"({width:g} × {height:g} m)")
        return {self.OUTPUT: dest_id}
