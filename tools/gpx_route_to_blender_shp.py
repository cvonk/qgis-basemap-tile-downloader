# -*- coding: utf-8 -*-
"""
GPX route → draped shapefile for Blender — a QGIS Processing algorithm.

Runs the whole by-hand chain in one go: reproject the track to the AOI's CRS,
densify it so draping has vertices to work with, sample Z from the DTM, and
write an ESRI Shapefile with the z-dimension kept.

    Reproject → Densify by interval → Drape (set Z from raster) → Save as .shp

WHY EACH STEP.

  * REPROJECT, because Blender is told nothing about CRSs - BlenderGIS places
    the shapefile by raw coordinate. The track has to already be in the CRS the
    terrain was built in.
  * DENSIFY, because draping only sets Z AT VERTICES. A GPX leg can run hundreds
    of metres between fixes; without extra vertices the route cuts straight
    through ridges. Default interval follows the DTM's own pixel size.
  * DRAPE, because GPX elevation is unreliable twice over: consumer GPS vertical
    error is large, and many devices record ellipsoidal height while every
    national DTM is orthometric - about 50 m apart in the Dolomites. Take Z from
    the raster, never from the GPS.
  * OFFSET lifts the line clear of the mesh. The terrain in Blender is a 65x65
    base at subsurf 6, i.e. a SMOOTHED version of the raster, so a route draped
    exactly on the raster sinks into it wherever the mesh cuts a corner. 2 m is
    the usual clearance; raise it if the route still clips.

DRAPE ON THE SAME RASTER THE MESH IS DISPLACED BY. The old advice - "always the
DTM" - was right only while the mesh was displaced by the DTM too. Displace the
mesh with a surface model and drape the route on bare earth and the route ends
up UNDER the canopy by the tree height. Whatever raster feeds the Displace
modifier is the one to pass here.

Needs GDAL, which QGIS ships. Drop in a profile's processing/scripts/ folder;
appears under Scripts ▸ Blender.
"""
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterCrs, QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterLayer,
    QgsVectorFileWriter, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication

import processing

# The GPX carries no useful Z, so a nodata hit must not become a plausible height.
# Anything well outside real terrain works; the drape leaves it in place and it is
# obvious in Blender rather than silently blending in.
DEFAULT_NODATA = 500.0
DEFAULT_OFFSET = 2.0


class GpxRouteToBlenderShp(QgsProcessingAlgorithm):
    INPUT    = "INPUT"
    DTM      = "DTM"
    TARGET   = "TARGET_CRS"
    INTERVAL = "INTERVAL"
    OFFSET   = "OFFSET"
    NODATA   = "NODATA"
    OUTPUT   = "OUTPUT"

    def createInstance(self):
        return GpxRouteToBlenderShp()

    def name(self):
        return "gpx_route_to_blender_shp"

    def displayName(self):
        return "GPX route → draped shapefile for Blender"

    def group(self):
        return "Blender"

    def groupId(self):
        # Lowercase, and it must match osm_features_to_blender_csv.py exactly: the
        # Toolbox groups by ID, not by the displayed group(), so "Blender" here and
        # "blender" there produced TWO folders both captioned "Blender".
        return "blender"

    def shortHelpString(self):
        return self.tr(
            "Reproject, densify and drape a GPX track onto a DTM, then write an ESRI "
            "Shapefile with Z for BlenderGIS.\n\n"
            "Load the .gpx and pick its <b>tracks</b> sublayer as the input.\n\n"
            "<b>Drape onto the same raster that displaces the terrain mesh.</b> If the "
            "mesh uses a surface model (DEM/DOM) and the route is draped on bare earth "
            "(DTM/DGM), the route ends up under the canopy.\n\n"
            "The interval defaults to the raster's pixel size, and the offset lifts the "
            "line clear of the smoothed Blender mesh.")

    def tr(self, s):
        return QCoreApplication.translate("GpxRouteToBlenderShp", s)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, "GPX track (the 'tracks' sublayer)",
            [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DTM, "Raster to drape onto (the one the mesh is displaced by)"))
        self.addParameter(QgsProcessingParameterCrs(
            self.TARGET, "Target CRS (leave empty to take the raster's)",
            optional=True))
        # Parent the distance to the RASTER, not to the CRS parameter: the CRS one
        # is optional and normally left empty, and an empty parent leaves the unit
        # label reading "<unknown>". Parenting to a raster layer is what QGIS's own
        # gdal:viewshed does.
        self.addParameter(QgsProcessingParameterDistance(
            self.INTERVAL, "Densify interval (0 = the raster's pixel size)",
            parentParameterName=self.DTM, defaultValue=0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.OFFSET, "Offset above the terrain (m)",
            QgsProcessingParameterNumber.Double, defaultValue=DEFAULT_OFFSET))
        self.addParameter(QgsProcessingParameterNumber(
            self.NODATA, "Z to use where the raster has no data",
            QgsProcessingParameterNumber.Double, defaultValue=DEFAULT_NODATA))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "Output shapefile", fileFilter="ESRI Shapefile (*.shp)"))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsSource(parameters, self.INPUT, context)
        dtm = self.parameterAsRasterLayer(parameters, self.DTM, context)
        if src is None:
            raise QgsProcessingException("No input track. Pick the GPX's 'tracks' sublayer.")
        if dtm is None:
            raise QgsProcessingException("No raster to drape onto.")

        crs = self.parameterAsCrs(parameters, self.TARGET, context)
        if not crs.isValid():
            crs = dtm.crs()
            feedback.pushInfo("Target CRS not given; using the raster's %s." % crs.authid())
        if crs != dtm.crs():
            feedback.pushWarning(
                "Target CRS %s differs from the raster's %s. The drape reprojects on the "
                "fly, but matching them avoids a resampling step."
                % (crs.authid(), dtm.crs().authid()))

        interval = self.parameterAsDouble(parameters, self.INTERVAL, context)
        if interval <= 0:
            interval = abs(dtm.rasterUnitsPerPixelX()) or 2.5
            feedback.pushInfo("Densify interval taken from the raster: %.3f m" % interval)
        offset = self.parameterAsDouble(parameters, self.OFFSET, context)
        nodata = self.parameterAsDouble(parameters, self.NODATA, context)
        out = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not out.lower().endswith(".shp"):
            out += ".shp"

        feedback.pushInfo("Input: %d feature(s) in %s"
                          % (src.featureCount(), src.sourceCrs().authid()))

        steps = [
            ("Reproject", "native:reprojectlayer",
             {"INPUT": parameters[self.INPUT], "TARGET_CRS": crs}),
            ("Densify", "native:densifygeometriesgivenaninterval",
             {"INTERVAL": interval}),
            ("Drape", "native:setzfromraster",
             {"RASTER": dtm, "BAND": 1, "NODATA": nodata, "SCALE": 1.0, "OFFSET": offset}),
        ]
        layer = None
        for i, (label, alg, extra) in enumerate(steps):
            if feedback.isCanceled():
                return {}
            feedback.setProgressText("%d/4  %s" % (i + 1, label))
            params = dict(extra)
            if layer is not None:
                params["INPUT"] = layer
            params["OUTPUT"] = "memory:"
            layer = processing.run(alg, params, context=context,
                                   feedback=feedback, is_child_algorithm=True)["OUTPUT"]
            feedback.setProgress((i + 1) * 20)

        draped = context.takeResultLayer(layer) if isinstance(layer, str) else layer
        if draped is None:
            raise QgsProcessingException("The drape produced no layer.")
        n = sum(len(g.constGet()) if g.constGet() else 0
                for g in (f.geometry() for f in draped.getFeatures()) if not g.isEmpty())
        feedback.pushInfo("Densified to roughly %d vertices at %.2f m spacing." % (n, interval))

        feedback.setProgressText("4/4  Write shapefile")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "ESRI Shapefile"
        opts.fileEncoding = "UTF-8"
        # LineStringZ explicitly: the writer otherwise follows the source, and a
        # shapefile written without Z drapes for nothing - Blender then places the
        # whole route at zero.
        opts.overrideGeometryType = QgsWkbTypes.LineString
        opts.includeZ = True
        opts.forceMulti = False
        res = QgsVectorFileWriter.writeAsVectorFormatV3(draped, out, context.transformContext(),
                                                        opts)
        if res[0] != QgsVectorFileWriter.NoError:
            raise QgsProcessingException("Could not write %s: %s" % (out, res[1]))
        feedback.pushInfo("Wrote %s" % out)
        feedback.pushInfo(
            "In Blender: GIS ▸ Import ▸ Shapefile, Elevation Source = Geometry, and the "
            "CRS you chose here (%s). If you are REPLACING an existing route, do not "
            "delete the curve - it carries the Factor End keys and the driver the camera "
            "reads. Overwrite its point Z instead." % crs.authid())
        return {self.OUTPUT: out}
