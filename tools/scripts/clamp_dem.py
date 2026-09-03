# -*- coding: utf-8 -*-
"""Clamp impossible elevations in a DEM/DTM GeoTIFF to nodata.

    python clamp_dem.py "…\\DEM Scratch\\fine.tif"                  -> fine_clean.tif
    python clamp_dem.py "…\\fine.tif" --in-place
    python clamp_dem.py "…\\fine.tif" --dry-run --range 0 3600

WHY THIS EXISTS. Bolzano's DOM carries a bad spot at roughly 694 965 E /
5 168 505 N (EPSG:32632). It has turned up in two AOIs so far - Seceda read
1 375 339 m there, Sassolungo 467 766 m across 4 pixels - because both
footprints contain it; the values differ only because each export resamples the
same defect onto a different grid. Confirmed to be in the SOURCE by re-requesting
that patch straight from the WCS, so it is not something the pipeline does.

Two things go wrong if it is left in:
  * QGIS stretches the display over the full range, so every real elevation
    lands in the bottom fraction of the ramp and the raster looks solid black.
  * Blender's Displace IGNORES the GDAL nodata tag and displaces the raw value,
    so 467 766 m becomes a 467 km spike. That is what the "Dimensions Z" sanity
    check in the HOWTO is for; this fixes the cause instead.

Out-of-range pixels become nodata rather than being interpolated, so the normal
gdal_fillnodata step in the merge deals with them like any other hole.

Needs the GDAL Python bindings that QGIS ships. Run from the OSGeo4W Shell.
"""

import argparse
import os
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# Alps: Mont Blanc is 4808 m, the Dolomites top out near 3350. Anything outside
# this is a defect, not terrain. Widen with --range for other ground.
DEFAULT_LO, DEFAULT_HI = 0.0, 3600.0
DEFAULT_NODATA         = -9999.0
BLOCK                  = 1024          # rows per pass, so a 12400^2 float fits easily


def describe(path):
    """Open the raster and report what it is, without loading it all."""
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    print("input     : %s" % os.path.basename(path))
    print("            %d x %d, %s, nodata %s"
          % (ds.RasterXSize, ds.RasterYSize, gdal.GetDataTypeName(b.DataType), nd))
    return ds, b, nd


def scan(band, nd, lo, hi, xsize, ysize, gt):
    """Count out-of-range pixels and where they are, in one pass."""
    bad_n, nodata_n, spots = 0, 0, []
    for y in range(0, ysize, BLOCK):
        rows = min(BLOCK, ysize - y)
        a = band.ReadAsArray(0, y, xsize, rows).astype("float64")
        isnd = (a == nd)
        bad = ~isnd & ((a < lo) | (a > hi) | ~np.isfinite(a))
        nodata_n += int(isnd.sum())
        n = int(bad.sum())
        if n:
            bad_n += n
            ys, xs = np.where(bad)
            for i in range(min(n, 6 - len(spots))):
                px, py = int(xs[i]), int(y + ys[i])
                spots.append((gt[0] + px * gt[1], gt[3] + py * gt[5], float(a[ys[i], xs[i]])))
    return bad_n, nodata_n, spots


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="elevation GeoTIFF")
    p.add_argument("-o", "--output", help="output (default: <stem>_clean.tif)")
    p.add_argument("--in-place", action="store_true", help="edit the input instead of copying")
    p.add_argument("--range", nargs=2, type=float, metavar=("LO", "HI"),
                   default=[DEFAULT_LO, DEFAULT_HI], help="plausible elevations, in metres")
    p.add_argument("--nodata", type=float, help="override the file's nodata value")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = p.parse_args(argv)

    lo, hi = a.range
    ds, band, nd = describe(a.input)
    if a.nodata is not None:
        nd = a.nodata
    if nd is None:
        nd = DEFAULT_NODATA
        print("            no nodata tag; assuming %s" % nd)
    gt = ds.GetGeoTransform()
    xsize, ysize = ds.RasterXSize, ds.RasterYSize

    mn, mx, _mean, _sd = band.ComputeStatistics(False)   # exact, never from overviews
    print("range     : %.2f .. %.2f m" % (mn, mx))
    bad_n, nodata_n, spots = scan(band, nd, lo, hi, xsize, ysize, gt)
    total = xsize * ysize
    print("nodata    : %.2f%% of pixels" % (100.0 * nodata_n / total))
    print("out of %.0f..%.0f m : %d pixel(s)" % (lo, hi, bad_n))
    for ex, ny, v in spots:
        print("            %.0f E, %.0f N  = %.2f m" % (ex, ny, v))
    if bad_n and mx > hi:
        span = mx - mn
        frac = 100.0 * (hi - mn) / span if span > 0 else 0.0
        print("            these squash all real terrain into the bottom %.1f%% of the"
              " display stretch" % frac)
    ds = None

    if a.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    if not bad_n:
        print("\nnothing to clamp; leaving %s alone" % os.path.basename(a.input))
        return 0

    # KEEP THE SOURCE DATASET ALIVE. A band outlives its dataset only if something
    # still references the dataset; `gdal.Open(x).GetRasterBand(1)` collects the
    # dataset immediately and every later read fails inside gdal_array.
    if a.in_place:
        out = a.input
        src = dst = gdal.Open(out, gdal.GA_Update)
    else:
        out = a.output or (os.path.splitext(a.input)[0] + "_clean.tif")
        src = gdal.Open(a.input)
        # Float32 => no PREDICTOR=2; byte-wise differencing is wrong for float samples.
        dst = gdal.GetDriverByName("GTiff").Create(
            out, xsize, ysize, 1, src.GetRasterBand(1).DataType,
            options=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
        dst.SetGeoTransform(src.GetGeoTransform())
        dst.SetProjection(src.GetProjection())

    sb = src.GetRasterBand(1)
    ob = dst.GetRasterBand(1)
    ob.SetNoDataValue(nd)
    for y in range(0, ysize, BLOCK):
        rows = min(BLOCK, ysize - y)
        arr = sb.ReadAsArray(0, y, xsize, rows)
        bad = (arr != nd) & ((arr < lo) | (arr > hi) | ~np.isfinite(arr))
        if bad.any():
            arr[bad] = nd
        ob.WriteArray(arr, 0, y)
    ob.FlushCache()
    sb = ob = None
    dst = src = None

    chk = gdal.Open(out)
    cb = chk.GetRasterBand(1)
    nmn, nmx, nmean, _ = cb.ComputeStatistics(False)
    print("\nwrote %s" % out)
    print("result    : %.2f .. %.2f m, mean %.2f, nodata %s"
          % (nmn, nmx, nmean, cb.GetNoDataValue()))
    chk = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
