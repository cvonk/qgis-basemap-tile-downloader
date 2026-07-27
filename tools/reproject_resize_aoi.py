# -*- coding: utf-8 -*-
"""
Reproject & resize an AOI — a QGIS Processing algorithm.

Takes an area of interest in any CRS, reprojects its *centre* to a target CRS,
and builds a fresh axis-aligned rectangle of a fixed size (default 16500 m) around
that centre — so the AOI keeps its location but becomes a clean, straight box in
the target CRS's grid (no rotation from meridian convergence, no odd fractional
extent). The output is a single-polygon layer you can feed straight into the
DGM/ortho tools or the plugin as your export extent.

Centre is preserved by transforming the source extent's centre point to the target
CRS (not by reprojecting the whole rectangle and taking its bbox), so it stays put
even when the two CRSes are rotated relative to each other.

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸ Area of
interest.
"""
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterExtent, QgsProcessingParameterNumber,
    QgsProcessingParameterCrs, QgsProcessingParameterFeatureSink,
    QgsCoordinateTransform, QgsProject,
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
            "and height in metres (both default 16500 m — set them equal for a "
            "square). Output is a one-polygon layer usable directly as an export "
            "extent for the DGM/ortho tools or the plugin.\n\nWidth/height are "
            "interpreted in the target CRS's units — use a projected CRS (metres), "
            "not a geographic one (degrees)."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(
            self.AOI, "Area of interest (any CRS)"))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Target CRS", defaultValue="EPSG:32632"))
        self.addParameter(QgsProcessingParameterNumber(
            self.WIDTH, "Width — West↔East (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=16500.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.HEIGHT, "Height — North↔South (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=16500.0, minValue=1.0))
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

        src_crs = self.parameterAsExtentCrs(parameters, self.AOI, context)
        src_rect = self.parameterAsExtent(parameters, self.AOI, context)
        if src_rect.isEmpty():
            raise QgsProcessingException("The area of interest is empty.")

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
