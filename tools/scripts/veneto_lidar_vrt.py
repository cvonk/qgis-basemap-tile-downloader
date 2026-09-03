# -*- coding: utf-8 -*-
"""Veneto LiDAR .asc tiles -> VRT -> clipped GeoTIFF, for the OSGeo4W Shell.

The Regione Veneto downloader (idt2.regione.veneto.it/idt/downloader/download,
"DTM LiDAR 5 m") hands you one zipped ESRI ASCII grid per 2 km tile and NO .prj,
so nothing downstream knows where the tiles are. This mosaics them, gives them
the CRS they are actually in, and optionally reprojects and clips to an AOI.

    python veneto_lidar_vrt.py "U:\\...\\DEM Scratch 31k\\dl"
    python veneto_lidar_vrt.py "...\\dl" --out fine.tif --te 730726 5143419 761726 5174419

THE CRS IS NOT IN THE FILES. The downloader calls it "Fuso 12", which is
EPSG:7795 (RDN2008 / Zone 12 (E-N)): central meridian 12E, false easting
3 000 000 - which is why the eastings start with a 3 and match no UTM zone. Its
twin EPSG:6876 is the same projection declared northing-first; the .asc stores
easting first, so 7795 is the one that keeps GDAL honest. Detected from the
easting here, overridable with --src-crs.

Everything runs through the GDAL Python API rather than shelling out to
gdalbuildvrt/gdalwarp, because the paths in this project are full of spaces
("Lago di Sorapis", "DEM Scratch 31k") and cmd.exe quoting eats them.

Needs the OSGeo4W Shell (or any Python with GDAL). Nothing to do with the
plugin; it just lives here so it is under version control.
"""

import argparse
import glob
import os
import sys

from osgeo import gdal, osr

gdal.UseExceptions()

# Downloader products are 400x400 at 5 m. A header that disagrees is worth a look.
EXPECT_CELLSIZE = 5.0
NODATA          = -9999.0
# Absurd values wreck the QGIS stretch AND become kilometre spikes under Blender's
# Displace, which ignores the nodata tag. Bolzano's DOM ships two pixels at
# 1 375 339 m; assume Veneto can too. Alpine ground fits well inside this.
CLAMP_LO, CLAMP_HI = 0.0, 4000.0
# false-easting megametre -> the CRS that uses it
FUSO_BY_EASTING = {3: ("EPSG:7795", "RDN2008 / Zone 12 (E-N), 'Fuso 12'")}


def read_header(path):
    """The 6-line ESRI ASCII header as a dict, normalised to lower-left CORNER."""
    h = {}
    with open(path, "r") as fh:
        for _ in range(6):
            parts = fh.readline().split()
            if len(parts) != 2:
                raise ValueError("%s: unreadable header" % os.path.basename(path))
            h[parts[0].lower()] = float(parts[1])
    cs = h["cellsize"]
    # xllcenter/yllcenter (what these tiles use) is the CENTRE of the corner cell,
    # half a pixel in from xllcorner. Getting this wrong shifts every tile by 2.5 m.
    if "xllcenter" in h:
        h["xllcorner"] = h["xllcenter"] - cs / 2.0
        h["yllcorner"] = h["yllcenter"] - cs / 2.0
    return h


def scan(tiledir):
    """Headers of every .asc in tiledir, plus the extent they span."""
    files = sorted(glob.glob(os.path.join(tiledir, "*.asc")))
    if not files:
        raise SystemExit("no .asc files in %s (did you unzip them?)" % tiledir)
    heads, sizes = [], set()
    for f in files:
        h = read_header(f)
        heads.append((f, h))
        sizes.add((h["cellsize"], int(h["ncols"]), int(h["nrows"])))
    x0 = min(h["xllcorner"] for _f, h in heads)
    y0 = min(h["yllcorner"] for _f, h in heads)
    x1 = max(h["xllcorner"] + h["ncols"] * h["cellsize"] for _f, h in heads)
    y1 = max(h["yllcorner"] + h["nrows"] * h["cellsize"] for _f, h in heads)
    return files, heads, sizes, (x0, y0, x1, y1)


def detect_crs(x0, x1):
    """Which Fuso the eastings belong to, or None if they look like something else.

    ROUND, do not floor: the false easting IS the central meridian, so tiles west
    of it sit just below the megametre mark - this AOI starts at 2 998 000, which
    floors to 2 and would go undetected."""
    return FUSO_BY_EASTING.get(int(round((x0 + x1) / 2.0 / 1000000.0)))


def reproject_box(box, src, dst):
    """A bounding box moved between CRSs, sampling the edges so rotation is kept."""
    s, d = osr.SpatialReference(), osr.SpatialReference()
    s.SetFromUserInput(src); d.SetFromUserInput(dst)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    d.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(s, d)
    x0, y0, x1, y1 = box
    pts = []
    for i in range(11):                       # edge samples, not just the 4 corners
        t = i / 10.0
        pts += [(x0 + (x1 - x0) * t, y0), (x0 + (x1 - x0) * t, y1),
                (x0, y0 + (y1 - y0) * t), (x1, y0 + (y1 - y0) * t)]
    out = [tr.TransformPoint(px, py)[:2] for px, py in pts]
    xs = [p[0] for p in out]; ys = [p[1] for p in out]
    return min(xs), min(ys), max(xs), max(ys)


def clamp(path, lo, hi):
    """Out-of-range pixels -> nodata, in place. Returns how many were caught."""
    import numpy as np
    ds = gdal.Open(path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    nd = band.GetNoDataValue()
    nd = NODATA if nd is None else nd
    fixed, step = 0, 1024
    for y in range(0, ds.RasterYSize, step):
        rows = min(step, ds.RasterYSize - y)
        a = band.ReadAsArray(0, y, ds.RasterXSize, rows)
        bad = (a != nd) & ((a < lo) | (a > hi) | ~np.isfinite(a))
        n = int(bad.sum())
        if n:
            a[bad] = nd
            band.WriteArray(a, 0, y)
            fixed += n
    band.SetNoDataValue(nd)
    band.FlushCache()
    ds = None
    return fixed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tiledir", help="folder of unzipped .asc tiles")
    p.add_argument("--vrt", help="VRT to write (default: <tiledir>/../veneto_lidar.vrt)")
    p.add_argument("--out", help="also warp to this GeoTIFF")
    p.add_argument("--src-crs", help="override the detected source CRS, e.g. EPSG:7795")
    p.add_argument("--t-crs", default="EPSG:32632", help="target CRS for --out")
    p.add_argument("--te", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                   help="target extent for --out, in --t-crs (the AOI box)")
    p.add_argument("--tr", type=float, help="target pixel size (default: the tiles' own)")
    p.add_argument("--resample", default="bilinear", help="warp resampling (default bilinear)")
    p.add_argument("--clamp", nargs=2, type=float, metavar=("LO", "HI"),
                   default=[CLAMP_LO, CLAMP_HI], help="sane elevation range for --out")
    p.add_argument("--no-clamp", action="store_true", help="keep out-of-range values")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = p.parse_args(argv)

    tiledir = os.path.abspath(a.tiledir)
    files, _heads, sizes, box = scan(tiledir)
    x0, y0, x1, y1 = box
    print("tiles     : %d .asc in %s" % (len(files), tiledir))
    for cs, nc, nr in sorted(sizes):
        print("            %dx%d at %.2f m = %.0f m square" % (nc, nr, cs, nc * cs))
    if len(sizes) > 1:
        print("  WARNING : tiles are not all the same shape; check the download")
    cell = sorted(sizes)[0][0]
    if abs(cell - EXPECT_CELLSIZE) > 1e-6:
        print("  note    : expected %.1f m cells for the LiDAR 5 m product" % EXPECT_CELLSIZE)
    print("extent    : %.0f..%.0f E, %.0f..%.0f N  (%.1f x %.1f km)"
          % (x0, x1, y0, y1, (x1 - x0) / 1000.0, (y1 - y0) / 1000.0))

    if a.src_crs:
        src_crs, why = a.src_crs, "given with --src-crs"
    else:
        found = detect_crs(x0, x1)
        if not found:
            raise SystemExit(
                "cannot tell the CRS from an easting of %.0f - pass --src-crs. The Veneto\n"
                "downloader's 'Fuso 12' is EPSG:7795." % x0)
        src_crs, why = found[0], found[1]
    print("source CRS: %s  (%s)" % (src_crs, why))

    vrt = a.vrt or os.path.join(os.path.dirname(tiledir), "veneto_lidar.vrt")

    if a.te:
        te = [float(v) for v in a.te]
        got = reproject_box(box, src_crs, a.t_crs)
        ox = max(0.0, min(got[2], te[2]) - max(got[0], te[0]))
        oy = max(0.0, min(got[3], te[3]) - max(got[1], te[1]))
        area = (te[2] - te[0]) * (te[3] - te[1])
        pct = 100.0 * ox * oy / area if area > 0 else 0.0
        print("tiles, %s: %.0f..%.0f E, %.0f..%.0f N"
              % (a.t_crs, got[0], got[2], got[1], got[3]))
        print("AOI          : %.0f..%.0f E, %.0f..%.0f N" % (te[0], te[2], te[1], te[3]))
        print("coverage  : %.0f%% of the AOI box" % pct)
        if pct < 99.0:
            print("  WARNING : the tiles do not fill the AOI. Download the neighbouring comuni,")
            print("            drop them in the same folder and re-run - nothing else changes.")

    if a.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    gdal.BuildVRT(vrt, files, options=gdal.BuildVRTOptions(
        outputSRS=src_crs, srcNodata=NODATA, VRTNodata=NODATA, resolution="highest"))
    print("\nwrote %s" % vrt)

    if not a.out:
        print("(no --out, so no GeoTIFF; load the VRT in QGIS or pass --out)")
        return 0

    # Float32 => no PREDICTOR=2: byte-wise differencing is wrong for float samples.
    wo = dict(dstSRS=a.t_crs, srcNodata=NODATA, dstNodata=NODATA,
              resampleAlg=a.resample, creationOptions=["COMPRESS=DEFLATE", "TILED=YES",
                                                       "BIGTIFF=IF_SAFER"])
    if a.te:
        wo["outputBounds"] = [float(v) for v in a.te]
    if a.tr:
        wo["xRes"] = wo["yRes"] = a.tr
    gdal.Warp(a.out, vrt, options=gdal.WarpOptions(**wo))
    print("wrote %s" % a.out)

    if not a.no_clamp:
        n = clamp(a.out, a.clamp[0], a.clamp[1])
        print("clamped %d pixel(s) outside %.0f..%.0f m to nodata" % (n, a.clamp[0], a.clamp[1]))

    ds = gdal.Open(a.out)
    b = ds.GetRasterBand(1)
    mn, mx, mean, _sd = b.ComputeStatistics(False)      # exact, never from overviews
    print("result    : %d x %d, %s, %.2f..%.2f m, mean %.2f, nodata %s"
          % (ds.RasterXSize, ds.RasterYSize, gdal.GetDataTypeName(b.DataType),
             mn, mx, mean, b.GetNoDataValue()))
    ds = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
