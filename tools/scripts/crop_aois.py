# -*- coding: utf-8 -*-
"""Crop every per-AOI GeoTIFF referenced in a QGIS project to its AOI square,
overwriting in place and backing up each original ONCE into _backup_precrop/.

A project-specific utility kept for reference/reuse (it encodes a naming
convention: rasters named "<Area> - ..." are cropped to "<Area> - AOI (32632)"),
NOT a general tool or a Processing algorithm. Adapt the constants for another
project. Run with QGIS CLOSED (Windows locks loaded rasters).

  python crop_aois.py --project "C:/path/to/project.qgz" --dry-run
  python crop_aois.py --project "C:/path/to/project.qgz"
  python crop_aois.py --project "C:/path/to/project.qgz" --only "Drei Zinnen - DTM"

Robust + resumable: the backup is the pristine original, and each crop reads FROM
the backup, so re-running always re-crops the original (never a crop-of-a-crop).
Needs the GDAL Python bindings — run it with the QGIS Python (python-qgis-ltr).
"""
import argparse
import html
import os
import re
import shutil

from osgeo import gdal, ogr, osr
gdal.UseExceptions()

# Set from --project in main().
PROJ = PROJECT = BACKUP_ROOT = LAGO_CUTLINE = None
SIZE = 16500.0                                    # AOI side length, metres
HALF = SIZE / 2.0

# Lago di Sorapis' AOI is a memory layer (no shapefile), so its cutline is a
# generated square about its (resize-preserved) centre. Project-specific.
LAGO = "Lago di Sorapis"
LAGO_CENTRE = (746226.383, 5158919.575)           # EPSG:32632


def project_xml():
    """The project's .qgs XML, whether the project is a .qgs or a zipped .qgz."""
    if PROJECT.lower().endswith(".qgs"):
        return open(PROJECT, encoding="utf-8").read()
    import zipfile
    with zipfile.ZipFile(PROJECT) as z:
        name = next(n for n in z.namelist() if n.endswith(".qgs"))
        return z.read(name).decode("utf-8")


def aoi_area(name):
    return name.split(" - ")[0].strip()


def per_aoi_rasters():
    """(area, name, abspath) for every gdal raster whose area matches an AOI."""
    s = project_xml()
    rasters, aoi_areas = [], set()
    for m in re.finditer(r"<maplayer\b.*?</maplayer>", s, re.S):
        blk = m.group(0)
        nm = re.search(r"<layername>(.*?)</layername>", blk)
        ds = re.search(r"<datasource>(.*?)</datasource>", blk)
        pv = re.search(r"<provider[^>]*>(.*?)</provider>", blk)
        ty = re.search(r'type="([^"]+)"', blk[:200])
        if not (nm and ds):
            continue
        name, prov = html.unescape(nm.group(1)), (pv.group(1) if pv else "")
        dsrc = html.unescape(ds.group(1))
        if "AOI" in name and prov in ("ogr", "memory"):
            aoi_areas.add(name.split(" - AOI")[0].strip())
        if (ty and ty.group(1) == "raster") and prov == "gdal":
            rasters.append((name, dsrc))
    out = []
    for name, dsrc in rasters:
        a = aoi_area(name)
        if a in aoi_areas:
            p = dsrc if os.path.isabs(dsrc) else os.path.normpath(
                os.path.join(PROJ, dsrc.replace("/", os.sep)))
            out.append((a, name, p))
    return sorted(out)


def make_square_cutline(path, cx, cy, epsg=32632):
    if os.path.exists(path):
        ogr.GetDriverByName("GPKG").DeleteDataSource(path)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(epsg)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("aoi", srs, ogr.wkbPolygon)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in [(cx - HALF, cy - HALF), (cx + HALF, cy - HALF),
                 (cx + HALF, cy + HALF), (cx - HALF, cy + HALF),
                 (cx - HALF, cy - HALF)]:
        ring.AddPoint(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(poly); lyr.CreateFeature(f)
    f = lyr = ds = None


def cutline_for(area):
    if area == LAGO:
        return LAGO_CUTLINE
    return os.path.join(PROJ, "Area", area, "Area of Interest",
                        f"{area} - AOI (32632).shp")


def backup_path(abspath):
    return os.path.join(BACKUP_ROOT, os.path.relpath(abspath, PROJ))


def crop_one(area, name, abspath, dry):
    if not os.path.isfile(abspath):
        return "MISSING", "file not found"
    src_for_read = backup_path(abspath)                # crop FROM the pristine copy
    ds = gdal.Open(abspath if not os.path.isfile(src_for_read) else src_for_read)
    gt = ds.GetGeoTransform()
    xres, yres = abs(gt[1]), abs(gt[5])
    bands = ds.RasterCount
    b1 = ds.GetRasterBand(1)
    nd = b1.GetNoDataValue()
    is_float = b1.DataType in (gdal.GDT_Float32, gdal.GDT_Float64)
    ds = None
    cut = cutline_for(area)
    kind = ("single-band" if bands == 1 else f"{bands}-band")
    plan = (f"{name}: {kind}, {xres:g} m, cutline={os.path.basename(cut)} "
            f"-> {'nodata '+str(nd if nd is not None else -9999) if bands == 1 else ('keep alpha' if bands >= 4 else 'add alpha')}")
    if dry:
        return "DRY", plan

    bpath = backup_path(abspath)                       # back up the original ONCE
    if not os.path.exists(bpath):
        os.makedirs(os.path.dirname(bpath), exist_ok=True)
        shutil.copy2(abspath, bpath)

    creation = ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    kw = dict(format="GTiff", cutlineDSName=cut, cropToCutline=True,
              xRes=xres, yRes=yres, targetAlignedPixels=True,
              resampleAlg="near", creationOptions=creation, multithread=True)
    if bands == 1:
        kw["dstNodata"] = nd if nd is not None else -9999.0
        creation.append("PREDICTOR=3" if is_float else "PREDICTOR=2")
    else:
        creation.append("PREDICTOR=2")
        if bands < 4:
            kw["dstAlpha"] = True                      # RGB -> add transparency
    tmp = abspath + ".cropping.tif"
    gdal.Warp(tmp, bpath, options=gdal.WarpOptions(**kw))
    # gdalwarp drops overviews; rebuild internal ones (AVERAGE, 2/4/8/16) so the
    # file is complete. Done before the swap so what lands in place is finished.
    ods = gdal.Open(tmp, gdal.GA_Update)
    ods.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ods = None
    os.replace(tmp, abspath)                           # atomic swap into place
    return "OK", plan


def main():
    global PROJ, PROJECT, BACKUP_ROOT, LAGO_CUTLINE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True,
                    help="path to the QGIS project (.qgz or .qgs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="substring filter on layer name")
    a = ap.parse_args()

    PROJECT = os.path.abspath(a.project)
    PROJ = os.path.dirname(PROJECT)
    BACKUP_ROOT = os.path.join(PROJ, "_backup_precrop")
    LAGO_CUTLINE = os.path.join(BACKUP_ROOT, "_lago_aoi_16500.gpkg")

    os.makedirs(BACKUP_ROOT, exist_ok=True)
    if not a.dry_run:
        make_square_cutline(LAGO_CUTLINE, *LAGO_CENTRE)

    rasters = per_aoi_rasters()
    if a.only:
        rasters = [r for r in rasters if a.only in r[1]]
    print(f"{'DRY-RUN: ' if a.dry_run else ''}{len(rasters)} raster(s), "
          f"cropping to {SIZE:.0f} m AOIs, backups in {BACKUP_ROOT}\n")
    tally = {}
    for area, name, p in rasters:
        try:
            status, msg = crop_one(area, name, p, a.dry_run)
        except Exception as e:                          # noqa: BLE001
            status, msg = "ERROR", str(e)
        tally[status] = tally.get(status, 0) + 1
        print(f"  [{status}] {msg}")
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))


if __name__ == "__main__":
    main()
