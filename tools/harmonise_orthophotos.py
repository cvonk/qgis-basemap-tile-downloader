# -*- coding: utf-8 -*-
"""
Harmonise & merge overlapping orthophotos — a QGIS Processing algorithm.

Colour-matches several overlapping orthophotos and composites them into one
seam-reduced GeoTIFF. It's the same seam colour-match the Basemap Tile Downloader
plugin does across a MapServer's per-year sublayers, but on separate rasters you
provide — e.g. Styria's per-flight-period DOP layers (Flug_2022_2024_RGB /
_2019_2021_RGB / _2016_2018_RGB), which are separate ImageServer services the
plugin can't harmonise in one pass.

ORDER THE INPUTS BEST IMAGERY FIRST. They are stacked in that order, first on
top, so the first layer's pixels win wherever it has data and the later ones only
fill the gaps it leaves. "Best" is your call per area: usually the newest flight,
but pick the sharper, cleaner or less cloudy sheet where they differ.

Separately — and this is the part that surprises people — the layer covering the
most of the area becomes the colour REFERENCE: its look is kept and the others
are matched to it per channel on the strip where they border it.

The two are INDEPENDENT. Order decides which PIXELS survive; coverage decides
which LOOK they are matched to, and that is not a parameter. So the reference can
sit at the BOTTOM of the stack, barely visible, while everything above it still
wears its colours - which is right: you want the small sheet you put first to
blend into the big one it borders, not the other way round.

The log states both, separately:

    Reference = input #2 (95% coverage); matching the others to it.
    Stacking (top first): #1 over #2

Use it to reduce the banding a provider's "current" mosaic shows between flight
years, or to butt one country's imagery against another's across a border.

Typical flow: download each period with the plugin (its ArcGIS source handles
ImageServers), then run this on the results. Needs GDAL + numpy (ship with QGIS).

Drop in a profile's processing/scripts/ folder; appears under Scripts ▸
Orthophoto.
"""
import os

from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
    QgsProcessingParameterExtent, QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination,
)

try:
    from osgeo import gdal, osr
    import numpy as np
    gdal.UseExceptions()
except ImportError:      # pragma: no cover
    gdal = None
    np = None

# Seam-statistics parameters (identical to the plugin's ArcGIS harmonise).
_STATS_MAXDIM = 2200     # read each layer at ≤ this for the seam statistics
_SEAM_PX = 6             # strip width (reduced-res px) either side of a seam
_MIN_SEAM = 400          # too few seam pixels → the layers don't really touch
METERS_PER_DEGREE = 111319.49079327358


class HarmoniseOrthophotos(QgsProcessingAlgorithm):
    INPUTS = "INPUTS"
    MATCH = "MATCH"
    RES = "RES"
    CRS = "CRS"
    CLIP = "CLIP"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return HarmoniseOrthophotos()

    def name(self):
        return "harmonise_orthophotos"

    def displayName(self):
        return "Harmonise & merge orthophotos (seam colour-match)"

    def group(self):
        return "Orthophoto"

    def groupId(self):
        return "orthophoto"

    def shortHelpString(self):
        return (
            "Colour-match several overlapping orthophotos and composite them into "
            "one seam-reduced GeoTIFF.\n\n"
            "<b>Add the BEST imagery first.</b> Layers are stacked in the order "
            "you list them, <b>first on top</b>, so the first layer's pixels win "
            "wherever it has data and the rest only fill the gaps it leaves. "
            "Usually that means newest first, but pick whichever is sharper, "
            "cleaner or less cloudy where they differ.\n\n"
            "Separately, the layer covering the <b>most of the area</b> becomes "
            "the colour <b>reference</b>: its look is kept and the others are "
            "matched to it where they border it.\n\n"
            "These are <b>independent</b>. Order decides which PIXELS survive; "
            "coverage decides which LOOK they take, and that is not a setting. So "
            "the reference may sit at the bottom of the stack, barely visible, "
            "while everything above it wears its colours — which is what you want, "
            "so the sheet you put first blends into the one it borders. The log "
            "reports both: <tt>Reference = input #N</tt> and "
            "<tt>Stacking (top first)</tt>.\n\n"
            "Reduces the banding a 'current' mosaic shows between "
            "flight years — e.g. Styria's Flug_2022_2024 / _2019_2021 / _2016_2018 "
            "DOP layers (download each with the plugin, then merge here).\n\n"
            "Match strength 0 keeps each layer's own brightness/contrast (match at "
            "the seam only); →1 also pulls their overall brightness/contrast "
            "together (more uniform, but can mute and slightly re-expose seams)."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUTS, "Orthophotos (best first — first one ends up on top)",
            layerType=QgsProcessing.TypeRaster))
        self.addParameter(QgsProcessingParameterNumber(
            self.MATCH, "Match brightness/contrast strength (0–1)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.RES, "Output resolution (m, 0 = finest input)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Output CRS", defaultValue="EPSG:32632"))
        self.addParameter(QgsProcessingParameterExtent(
            self.CLIP, "Clip to extent (optional; default = union of inputs)",
            optional=True))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, "Harmonised orthophoto"))

    # ── geometry helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _extent_in(ds, out_srs):
        """Raster ds's footprint as (xmin, ymin, xmax, ymax) in out_srs."""
        gt = ds.GetGeoTransform()
        w, h = ds.RasterXSize, ds.RasterYSize
        corners = [(gt[0], gt[3]), (gt[0] + w * gt[1], gt[3]),
                   (gt[0], gt[3] + h * gt[5]), (gt[0] + w * gt[1], gt[3] + h * gt[5])]
        src = ds.GetSpatialRef()
        if src is not None and not src.IsSame(out_srs):
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            out_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            ct = osr.CoordinateTransformation(src, out_srs)
            corners = [ct.TransformPoint(x, y)[:2] for x, y in corners]
        xs = [c[0] for c in corners]; ys = [c[1] for c in corners]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _native_res(ds, out_srs):
        """Pixel size of ds expressed in out_srs metres (approx via footprint)."""
        xmin, ymin, xmax, ymax = HarmoniseOrthophotos._extent_in(ds, out_srs)
        return min((xmax - xmin) / ds.RasterXSize, (ymax - ymin) / ds.RasterYSize)

    # ── seam colour match (ported from the plugin's arcgis backend) ──────────
    @staticmethod
    def _read_reduced(path):
        ds = gdal.Open(path)
        W, H = ds.RasterXSize, ds.RasterYSize
        scale = max(1.0, max(W, H) / _STATS_MAXDIM)
        w, h = max(1, round(W / scale)), max(1, round(H / scale))
        m = gdal.Translate("", ds, format="MEM", width=w, height=h, resampleAlg="average")
        rgb = np.dstack([m.GetRasterBand(i + 1).ReadAsArray().astype(np.float64)
                         for i in range(3)])
        alpha = m.GetRasterBand(4).ReadAsArray() if m.RasterCount >= 4 \
            else np.full((h, w), 255)
        return rgb, alpha > 0

    @staticmethod
    def _dilate(mask, it):
        for _ in range(it):
            d = mask.copy()
            d[1:] |= mask[:-1]; d[:-1] |= mask[1:]
            d[:, 1:] |= mask[:, :-1]; d[:, :-1] |= mask[:, 1:]
            mask = d
        return mask

    @classmethod
    def _seam_gains(cls, rgb_ref, m_ref, rgb_oth, m_oth):
        """Per-channel (gain, offset) mapping `oth` onto `ref`, fit on the strips
        either side of their shared boundary. Identity if they don't meet."""
        seam_o = m_oth & cls._dilate(m_ref, _SEAM_PX)
        seam_r = m_ref & cls._dilate(m_oth, _SEAM_PX)
        if seam_o.sum() < _MIN_SEAM or seam_r.sum() < _MIN_SEAM:
            return [(1.0, 0.0)] * 3, 0
        out = []
        for c in range(3):
            ro, rr = rgb_oth[..., c][seam_o], rgb_ref[..., c][seam_r]
            g = min(2.0, max(0.5, (rr.std() or 1.0) / (ro.std() or 1.0)))
            out.append((g, rr.mean() - g * ro.mean()))
        return out, int(min(seam_o.sum(), seam_r.sum()))

    @staticmethod
    def _global_gains(rgb_ref, m_ref, rgb_oth, m_oth):
        out = []
        for c in range(3):
            ro, rr = rgb_oth[..., c][m_oth], rgb_ref[..., c][m_ref]
            g = min(2.0, max(0.5, (rr.std() or 1.0) / (ro.std() or 1.0)))
            out.append((g, rr.mean() - g * ro.mean()))
        return out

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None or np is None:
            raise QgsProcessingException("GDAL/numpy are unavailable.")
        layers = self.parameterAsLayerList(parameters, self.INPUTS, context)
        if len(layers) < 2:
            raise QgsProcessingException("Add at least two orthophotos to merge.")
        paths = [lyr.source() for lyr in layers]      # newest first, as added
        strength = self.parameterAsDouble(parameters, self.MATCH, context)
        res = self.parameterAsDouble(parameters, self.RES, context)
        out_crs = self.parameterAsCrs(parameters, self.CRS, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        out_srs = osr.SpatialReference(); out_srs.SetFromUserInput(out_crs.authid())

        # Common grid: the clip extent (if given) or the union of the inputs, at
        # the requested resolution (or the finest input's, expressed in out_crs).
        dss = [gdal.Open(p) for p in paths]
        if any(d is None for d in dss):
            raise QgsProcessingException("An input raster could not be opened.")
        # _native_res measures the footprint in out_srs, so the auto value is
        # already in the output CRS's units. A resolution the user typed is
        # metres, and gdal.Warp reads xRes/yRes in output-CRS units — so for a
        # geographic CRS it must be converted, or 1 m is taken as 1 DEGREE and
        # the warp collapses to a couple of pixels.
        if res <= 0:
            res = min(self._native_res(d, out_srs) for d in dss)
        elif out_crs.isGeographic():
            deg = res / METERS_PER_DEGREE
            feedback.pushInfo(f"Output CRS {out_crs.authid()} is geographic: "
                              f"{res:g} m → {deg:.8f}° per pixel.")
            res = deg
        clip = self.parameterAsExtent(parameters, self.CLIP, context, out_crs)
        if clip.isEmpty():
            exts = [self._extent_in(d, out_srs) for d in dss]
            te = (min(e[0] for e in exts), min(e[1] for e in exts),
                  max(e[2] for e in exts), max(e[3] for e in exts))
        else:
            te = (clip.xMinimum(), clip.yMinimum(), clip.xMaximum(), clip.yMaximum())
        unit = "°" if out_crs.isGeographic() else "m"
        feedback.pushInfo(f"Output grid: {out_crs.authid()} @ {res:g} {unit}, "
                          f"extent {te[0]:.0f},{te[1]:.0f},{te[2]:.0f},{te[3]:.0f}")

        # 1) Warp every input onto that identical grid as RGBA (aligned so the
        #    reduced reads and the composite line up pixel-for-pixel).
        tmp = os.path.join(os.path.dirname(out_path), "_harm_tmp")
        os.makedirs(tmp, exist_ok=True)
        aligned = []
        for i, p in enumerate(paths):
            if feedback.isCanceled():
                return {}
            a = os.path.join(tmp, f"aligned_{i}.tif")
            gdal.Warp(a, p, options=gdal.WarpOptions(
                format="GTiff", dstSRS=out_crs.authid(), outputBounds=te,
                xRes=res, yRes=res, targetAlignedPixels=True,
                resampleAlg="bilinear", srcAlpha=True, dstAlpha=True,
                creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]))
            aligned.append(a)
            feedback.setProgress(60.0 * (i + 1) / len(paths))

        # 2) Reference = the layer covering the most of the grid.
        reduced = [self._read_reduced(a) for a in aligned]
        cover = [m.mean() for _, m in reduced]
        ref = max(range(len(aligned)), key=lambda i: cover[i])
        feedback.pushInfo(f"Reference = input #{ref + 1} ({cover[ref] * 100:.0f}% "
                          f"coverage); matching the others to it.")

        # 3) Colour-match each non-reference layer to the reference at their seam.
        adjusted = [None] * len(aligned)
        for i, a in enumerate(aligned):
            if i == ref:
                adjusted[i] = a
                continue
            gains, npx = self._seam_gains(reduced[ref][0], reduced[ref][1],
                                          reduced[i][0], reduced[i][1])
            adj = os.path.join(tmp, f"adj_{i}.tif")
            if npx == 0:
                feedback.pushInfo(f"Input #{i + 1} does not border the reference — "
                                  f"left as-is.")
                gdal.Translate(adj, a, options=gdal.TranslateOptions(format="GTiff"))
            else:
                if strength > 0:
                    gg = self._global_gains(reduced[ref][0], reduced[ref][1],
                                            reduced[i][0], reduced[i][1])
                    gains = [((1 - strength) * gl + strength * ggl,
                              (1 - strength) * ol + strength * ogl)
                             for (gl, ol), (ggl, ogl) in zip(gains, gg)]
                sp = [[0, 255, o, o + 255 * g] for (g, o) in gains] + [[0, 255, 0, 255]]
                feedback.pushInfo(f"Input #{i + 1} → reference on {npx} seam px "
                                  f"(match={strength * 100:.0f}%), gains="
                                  f"{[f'{g:.2f}+{o:.0f}' for g, o in gains]}")
                gdal.Translate(adj, a, options=gdal.TranslateOptions(
                    format="GTiff", bandList=[1, 2, 3, 4], scaleParams=sp,
                    outputType=gdal.GDT_Byte))
            adjusted[i] = adj
        feedback.setProgress(80)

        # 4) Composite (already aligned, so this is a straight alpha overlay).
        #
        # gdal.Warp lays sources down in order and the LAST one wins, so reverse
        # the input list to put input #1 on top. Stacking follows INPUT ORDER and
        # nothing else: which layer is the colour reference decides the LOOK, not
        # who covers whom. Those used to be the same thing here - the reference
        # was appended last - which silently buried input #1 whenever it was not
        # also the reference, e.g. a small Austrian sheet over a large Italian one.
        composite_inputs = list(reversed(adjusted))
        feedback.pushInfo("Stacking (top first): "
                          + " over ".join("#%d" % (i + 1) for i in range(len(aligned))))
        ds = gdal.Warp(out_path, composite_inputs, options=gdal.WarpOptions(
            format="GTiff", srcAlpha=True, dstAlpha=True, resampleAlg="near",
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES",
                             "BLOCKXSIZE=256", "BLOCKYSIZE=256", "BIGTIFF=IF_SAFER"]))
        if ds is None:
            raise QgsProcessingException("The composite warp produced no output.")
        xs, ys = ds.RasterXSize, ds.RasterYSize
        if xs < 2 or ys < 2:
            ds = None
            raise QgsProcessingException(
                f"The composite is a degenerate {xs} × {ys} px raster — the "
                f"resolution is too coarse for the extent in {out_crs.authid()}.")
        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
        ds = None
        feedback.setProgress(100)
        feedback.pushInfo(f"✓ {xs} × {ys} px, {out_crs.authid()} @ {res:g} m")
        return {self.OUTPUT: out_path}
