# -*- coding: utf-8 -*-
"""
De-haze & block-match an orthophoto — a QGIS Processing algorithm.

Fixes the two things that make one national-ortho AOI look nothing like the next
even though both came from the same service:

  * a **uniform haze cast** — the whole tile was flown through haze, so blue sits
    well above red everywhere and the image reads grey-teal instead of green;
  * an **acquisition-block seam** — the AOI straddles two flight blocks with
    different radiometry, so half the tile is hazier than the other half and a
    visible step runs across it.

Both are cured by mapping percentiles per channel. Give a reference raster (an
AOI you already like the look of) and the input is matched to it band by band on
p2/p50/p98; the mid-tone anchor matters, an endpoint-only stretch overshoots and
leaves a residual cast. If the AOI is split, set Block threshold and the hazier
block is first matched onto the cleaner one across a feathered boundary.

This is the single-raster counterpart to "Harmonise & merge orthophotos", which
colour-matches several *separate* overlapping rasters. Use this one when the
seam is *inside* one file.

Picking the block threshold: run once with it at 0 and read the blurred blue-cast
(B-R) percentiles from the log. What tells the two cases apart is the LOW end, not
how wide the spread is — both look similarly wide:

    p5 near 0 but p95 high   part of the AOI is clean and part is hazy: a seam.
                             Set the threshold about midway.
                             Tre Cime  p5 -0.3  p50 +6.8   p95 +22.7  ->  8
    p5 already well above 0  the whole tile is hazy: leave the threshold at 0.
                             Sorapis   p5 +4.4  p50 +16.4  p95 +24.4  ->  0

Detection is deliberately NOT automatic: a smooth land-cover chroma gradient
(snowfields warm, forest blue) scores almost as strongly as a real seam, so
auto-splitting would mangle clean rasters. Look at the numbers and decide.

Needs GDAL + numpy (both ship with QGIS). Drop in a profile's processing/scripts/
folder; appears under Scripts ▸ Orthophoto.
"""
import os

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)

try:
    from osgeo import gdal
    import numpy as np
    gdal.UseExceptions()
except ImportError:      # pragma: no cover
    gdal = None
    np = None

_ANALYSIS = 3000     # analysis read is this wide; percentiles come from it
_BLUR_DIV = 59       # blur radius for the cast field, as _ANALYSIS / this
_FEATHER  = 70       # blur radius for the block blend weight, as _ANALYSIS / this
_STRIP    = 512      # full-res rows processed per pass
_CORE     = 0.05     # blend weight outside [this, 1-this] counts as block interior


class DehazeOrtho(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    REFERENCE = "REFERENCE"
    SPLIT = "SPLIT"
    SATURATION = "SATURATION"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return DehazeOrtho()

    def name(self):
        return "dehaze_ortho"

    def displayName(self):
        return "De-haze & block-match an orthophoto"

    def group(self):
        return "Orthophoto"

    def groupId(self):
        return "orthophoto"

    def shortHelpString(self):
        return (
            "Remove an orthophoto's haze cast and, optionally, the seam between "
            "two acquisition blocks inside it — so one AOI matches the look of "
            "another from the same service.\n\n"
            "<b>Reference</b> — a raster whose look you want (leave empty to only "
            "fix the seam and keep the input's own levels). The input is matched "
            "to it per channel on p2/p50/p98.\n\n"
            "<b>Block threshold</b> — 0 treats the AOI as one acquisition. Run "
            "once at 0 and read the blurred blue-cast (B-R) percentiles from the "
            "log: if p5 sits near 0 while p95 is high, part of the tile is clean "
            "and part is hazy — a seam — so set the threshold about midway and "
            "the hazier block is matched onto the cleaner one across a feathered "
            "boundary. If p5 is already well above 0 the whole tile is hazy; "
            "leave it at 0.\n\n"
            "<b>Saturation</b> — 1.0 leaves colour alone; de-hazed alpine imagery "
            "usually wants 1.3–1.6.\n\n"
            "For colour-matching several separate overlapping rasters instead, use "
            "\"Harmonise & merge orthophotos\"."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT, "Orthophoto to correct"))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.REFERENCE, "Reference look (optional)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPLIT, "Block threshold on blue-cast B-R (0 = single acquisition)",
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0,
            minValue=-100.0, maxValue=100.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SATURATION, "Saturation (1.0 = unchanged)",
            type=QgsProcessingParameterNumber.Double, defaultValue=1.0,
            minValue=0.0, maxValue=3.0))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, "Corrected orthophoto"))

    # ── numpy-only helpers (no scipy; QGIS does not guarantee it) ────────────
    @staticmethod
    def _box(x, r):
        """Box blur of radius r via an integral image."""
        if r < 1:
            return x.astype(np.float32)
        h, w = x.shape
        p = np.pad(x.astype(np.float64), r, mode="edge")
        c = np.pad(np.cumsum(np.cumsum(p, 0), 1), ((1, 0), (1, 0)))
        k = 2 * r + 1
        s = c[k:k + h, k:k + w] - c[0:h, k:k + w] - c[k:k + h, 0:w] + c[0:h, 0:w]
        return (s / float(k * k)).astype(np.float32)

    @classmethod
    def _blur(cls, x, r):
        return cls._box(cls._box(x, r), r)          # two boxes ≈ a soft kernel

    @staticmethod
    def _pct(rgb, mask):
        return [tuple(np.percentile(rgb[i][mask], [2, 50, 98])) for i in range(3)]

    @staticmethod
    def _piecewise(x, src, dst):
        """Map x through src p2/p50/p98 onto dst's, linear in each half."""
        lo = (x - src[0]) * ((dst[1] - dst[0]) / max(src[1] - src[0], 1e-6)) + dst[0]
        hi = (x - src[1]) * ((dst[2] - dst[1]) / max(src[2] - src[1], 1e-6)) + dst[1]
        return np.where(x < src[1], lo, hi)

    @staticmethod
    def _upsample(low, y0, rows, height, width):
        """Bilinear-upsample rows [y0, y0+rows) of `low` to the full grid."""
        n = low.shape[0]
        ry = np.clip((np.arange(y0, y0 + rows) + 0.5) * n / height - 0.5, 0, n - 1)
        rx = np.clip((np.arange(width) + 0.5) * n / width - 0.5, 0, n - 1)
        yi = np.floor(ry).astype(int); yj = np.minimum(yi + 1, n - 1)
        xi = np.floor(rx).astype(int); xj = np.minimum(xi + 1, n - 1)
        wy = (ry - yi)[:, None]; wx = (rx - xi)[None, :]
        top = low[yi][:, xi] * (1 - wx) + low[yi][:, xj] * wx
        bot = low[yj][:, xi] * (1 - wx) + low[yj][:, xj] * wx
        return top * (1 - wy) + bot * wy

    @staticmethod
    def _saturate(rgb, f):
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        return lum + (rgb - lum) * f

    def _analyse(self, ds, feedback):
        """Reduced-resolution RGB + valid mask. Nearest, so the histogram tails
        survive — averaging pulls p2/p98 inward and weakens the match."""
        a = ds.ReadAsArray(buf_xsize=_ANALYSIS, buf_ysize=_ANALYSIS,
                           resample_alg=gdal.GRIORA_NearestNeighbour).astype(np.float32)
        if a.ndim == 2 or a.shape[0] < 3:
            raise QgsProcessingException("Expected an RGB(A) orthophoto (3+ bands).")
        valid = a[3] > 200 if a.shape[0] >= 4 else np.ones(a.shape[1:], bool)
        if not valid.any():
            raise QgsProcessingException("The raster has no opaque pixels.")
        return a[:3], valid

    def processAlgorithm(self, parameters, context, feedback):
        if gdal is None or np is None:
            raise QgsProcessingException("GDAL/numpy are unavailable.")
        lyr = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        ref = self.parameterAsRasterLayer(parameters, self.REFERENCE, context)
        split = self.parameterAsDouble(parameters, self.SPLIT, context)
        sat = self.parameterAsDouble(parameters, self.SATURATION, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        src = gdal.Open(lyr.source())
        if src is None:
            raise QgsProcessingException("The input raster could not be opened.")
        width, height, nbands = src.RasterXSize, src.RasterYSize, src.RasterCount
        feedback.pushInfo(f"Input: {width} × {height} px, {nbands} bands")

        rgb_a, valid = self._analyse(src, feedback)
        cast = self._blur(rgb_a[2] - rgb_a[0], max(1, _ANALYSIS // _BLUR_DIV))
        q = np.percentile(cast[valid], [5, 50, 95])
        feedback.pushInfo("Blue-cast (B-R), blurred: p5 %+.1f  p50 %+.1f  p95 %+.1f"
                          % tuple(q))

        # ── segment the acquisition blocks, if the user says there are two ──
        if split != 0.0:
            hazy = (cast > split) & valid
            frac = hazy.sum() / float(valid.sum())
            if frac < 0.02 or frac > 0.98:
                raise QgsProcessingException(
                    f"Block threshold {split:g} puts {frac * 100:.0f}% of the AOI in "
                    f"the hazy block — pick a value inside the B-R range above.")
            soft = np.clip(self._blur((cast > split).astype(np.float32),
                                      max(1, _ANALYSIS // _FEATHER)), 0.0, 1.0)
            core_h = (soft > 1 - _CORE) & valid      # interiors only, so the
            core_c = (soft < _CORE) & valid          # feather zone is excluded
            if core_h.sum() < 1000 or core_c.sum() < 1000:
                raise QgsProcessingException(
                    "The two blocks are too interleaved to match — the boundary "
                    "looks like land cover, not an acquisition seam.")
            hazy_p, clean_p = self._pct(rgb_a, core_h), self._pct(rgb_a, core_c)
            feedback.pushInfo("Hazy block  %.0f%% of AOI, p2/50/98: %s"
                              % (100 * frac, ["%.0f/%.0f/%.0f" % p for p in hazy_p]))
            feedback.pushInfo("Clean block %.0f%% of AOI, p2/50/98: %s"
                              % (100 * (1 - frac), ["%.0f/%.0f/%.0f" % p for p in clean_p]))
        else:
            soft = hazy_p = None
            clean_p = self._pct(rgb_a, valid)
            feedback.pushInfo("Single acquisition, p2/50/98: %s"
                              % ["%.0f/%.0f/%.0f" % p for p in clean_p])

        # ── target levels: the reference's, or the input's own (seam fix only) ──
        if ref is not None:
            rds = gdal.Open(ref.source())
            if rds is None:
                raise QgsProcessingException("The reference raster could not be opened.")
            r_rgb, r_valid = self._analyse(rds, feedback)
            target = self._pct(r_rgb, r_valid)
            feedback.pushInfo("Reference p2/50/98: %s"
                              % ["%.0f/%.0f/%.0f" % p for p in target])
            rds = None
        else:
            target = clean_p
            feedback.pushInfo("No reference — keeping the input's own levels.")

        # ── full-resolution pass ────────────────────────────────────────────
        dst = gdal.GetDriverByName("GTiff").Create(
            out_path, width, height, nbands, gdal.GDT_Byte,
            options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2",
                     "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"])
        if dst is None:
            raise QgsProcessingException(f"Could not create {out_path}.")
        dst.SetGeoTransform(src.GetGeoTransform())
        dst.SetProjection(src.GetProjection())
        for i in range(nbands):
            dst.GetRasterBand(i + 1).SetColorInterpretation(
                src.GetRasterBand(i + 1).GetColorInterpretation())

        for y in range(0, height, _STRIP):
            if feedback.isCanceled():
                dst = None
                return {}
            rows = min(_STRIP, height - y)
            tile = src.ReadAsArray(0, y, width, rows).astype(np.float32)
            out = tile[:3]
            if soft is not None:
                w = self._upsample(soft, y, rows, height, width)
                corr = np.stack([self._piecewise(out[i], hazy_p[i], clean_p[i])
                                 for i in range(3)])
                out = out * (1 - w) + corr * w
            out = np.stack([self._piecewise(out[i], clean_p[i], target[i])
                            for i in range(3)])
            if sat != 1.0:
                out = self._saturate(out, sat)
            out = np.clip(out, 0, 255).astype(np.uint8)
            for i in range(3):
                dst.GetRasterBand(i + 1).WriteArray(out[i], 0, y)
            for i in range(3, nbands):          # alpha / extra bands pass through
                dst.GetRasterBand(i + 1).WriteArray(tile[i].astype(np.uint8), 0, y)
            feedback.setProgress(95.0 * (y + rows) / height)

        dst.FlushCache()
        feedback.pushInfo("Building overviews…")
        dst.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32])
        dst = None
        src = None
        feedback.setProgress(100)
        feedback.pushInfo("✓ %s (%.0f MB)"
                          % (os.path.basename(out_path),
                             os.path.getsize(out_path) / 1e6))
        return {self.OUTPUT: out_path}
