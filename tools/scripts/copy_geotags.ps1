<#
.SYNOPSIS
    Copy the georeferencing from one raster onto another that has lost it.

.DESCRIPTION
    Photoshop - and most image editors - write a plain TIFF: the pixels survive
    but the GeoTIFF tags do not, so QGIS and Blender no longer know where the
    image belongs. This reads the CRS and geotransform from a reference raster
    (normally the file you opened in Photoshop) and stamps them onto the edited
    one.

    Only metadata is written. The pixels are never touched, so it is instant
    whatever the file size, and lossless whatever the compression - unlike
    re-exporting through gdal_translate, which rewrites every pixel.

    THE PIXEL GRID MUST MATCH. A geotransform maps pixel indices to ground
    coordinates, so it is only valid for the raster size it came from. Cropping,
    resizing or rotating in Photoshop invalidates it and the script refuses
    unless -Force.

    Needs the GDAL Python bindings - it drives them through the QGIS Python,
    which is found automatically.

.PARAMETER Reference
    The raster that still has its georeferencing.

.PARAMETER Target
    The edited raster to stamp. Modified IN PLACE unless -Output is given.

.PARAMETER Output
    Write to this new file instead of editing Target (Target is copied first).

.PARAMETER CopyNodata
    Also copy the reference's nodata value onto the target's bands.

.PARAMETER Force
    Proceed even when the pixel dimensions differ. The result will be
    geometrically wrong; only useful if you know the grids really do match.

.EXAMPLE
    .\copy_geotags.ps1 -Reference "ortho.tif" -Target "ortho_edited.tif"

.EXAMPLE
    .\copy_geotags.ps1 -Reference "ortho.tif" -Target "edited.tif" `
        -Output "edited_geo.tif" -CopyNodata
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string] $Reference,
    [Parameter(Mandatory)][string] $Target,
    [string] $Output,
    [string] $QgisPython,
    [switch] $CopyNodata,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ QGIS Python
if (-not $QgisPython) {
    # newest QGIS first; the LTR and non-LTR launchers have different names
    $cands = Get-ChildItem 'U:\Program Files', 'C:\Program Files' -Filter 'QGIS *' `
                -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    foreach ($q in $cands) {
        foreach ($n in 'python-qgis-ltr.bat', 'python-qgis.bat') {
            $p = Join-Path $q.FullName "bin\$n"
            if (Test-Path -LiteralPath $p) { $QgisPython = $p; break }
        }
        if ($QgisPython) { break }
    }
}
if (-not $QgisPython -or -not (Test-Path -LiteralPath $QgisPython)) {
    throw "QGIS Python not found. Pass -QgisPython <path to python-qgis*.bat>."
}

foreach ($f in @($Reference, $Target)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Not found: $f" }
}
$Reference = (Resolve-Path -LiteralPath $Reference).Path
$Target = (Resolve-Path -LiteralPath $Target).Path

# ------------------------------------------------------------------ target copy
$edit = $Target
if ($Output) {
    if ((Test-Path -LiteralPath $Output) -and -not $Force) {
        throw "Output already exists: $Output`nPass -Force to overwrite."
    }
    if ($PSCmdlet.ShouldProcess($Output, "copy $Target")) {
        Copy-Item -LiteralPath $Target -Destination $Output -Force
        $edit = (Resolve-Path -LiteralPath $Output).Path
    }
}

# ------------------------------------------------------------------ the worker
# A temp .py rather than `python -c`: multi-line -c does not survive the .bat.
$py = Join-Path ([System.IO.Path]::GetTempPath()) ("copy_geotags_{0}.py" -f [guid]::NewGuid())
@'
import sys
from osgeo import gdal, osr

gdal.UseExceptions()
ref_path, tgt_path, force, do_nodata = sys.argv[1], sys.argv[2], sys.argv[3] == "1", \
    sys.argv[4] == "1"

ref = gdal.Open(ref_path)
tgt = gdal.Open(tgt_path, gdal.GA_Update)

def describe(ds):
    srs = ds.GetSpatialRef()
    return {"size": (ds.RasterXSize, ds.RasterYSize), "bands": ds.RasterCount,
            "dtype": gdal.GetDataTypeName(ds.GetRasterBand(1).DataType),
            "gt": ds.GetGeoTransform(can_return_null=True),
            "epsg": srs.GetAuthorityCode(None) if srs else None,
            "srs": srs.GetName() if srs else None}

r, t = describe(ref), describe(tgt)
print("reference : %(size)s  %(bands)s band %(dtype)s  %(srs)s (EPSG:%(epsg)s)" % r)
print("target    : %(size)s  %(bands)s band %(dtype)s  %(srs)s (EPSG:%(epsg)s)" % t)

# exit 2 = a deliberate refusal (an expected outcome), not a crash
if r["gt"] is None:
    print("REFUSING: the reference has no geotransform - nothing to copy.")
    sys.exit(2)
if r["size"] != t["size"]:
    msg = ("pixel size differs: reference %s vs target %s - a geotransform is "
           "only valid for the grid it came from" % (r["size"], t["size"]))
    if not force:
        print("REFUSING: " + msg + ".")
        print("          Re-export from Photoshop at the original size, or pass "
              "-Force if you are certain the grids match.")
        sys.exit(2)
    print("WARNING: " + msg + " (forced)")
if r["bands"] != t["bands"]:
    print("note: band count differs (%d -> %d); georeferencing is unaffected"
          % (r["bands"], t["bands"]))

tgt.SetGeoTransform(r["gt"])
if ref.GetProjectionRef():
    tgt.SetProjection(ref.GetProjectionRef())
if do_nodata:
    nod = ref.GetRasterBand(1).GetNoDataValue()
    if nod is not None:
        for i in range(1, tgt.RasterCount + 1):
            tgt.GetRasterBand(i).SetNoDataValue(nod)
        print("nodata    : copied %s to %d band(s)" % (nod, tgt.RasterCount))
tgt.FlushCache()
tgt = None

check = describe(gdal.Open(tgt_path))
ok = (check["gt"] == r["gt"]
      and (check["epsg"] == r["epsg"] or ref.GetProjectionRef() == ""))
print("result    : %(size)s  %(srs)s (EPSG:%(epsg)s)" % check)
print("geotransform: %s" % (check["gt"],))
print("\n%s" % ("OK - georeferencing applied and verified."
                if ok else "FAILED - the tags did not stick."))
sys.exit(0 if ok else 1)
'@ | Set-Content -LiteralPath $py -Encoding UTF8

try {
    if ($PSCmdlet.ShouldProcess($edit, "stamp georeferencing from $Reference")) {
        & $QgisPython $py $Reference $edit $(if ($Force) { '1' } else { '0' }) `
            $(if ($CopyNodata) { '1' } else { '0' })
        if ($LASTEXITCODE -eq 2) {
            # a refusal, printed above - not an error worth a stack trace
            Write-Host ""
            Write-Host "  nothing written." -ForegroundColor Yellow
            return
        }
        if ($LASTEXITCODE -ne 0) { throw "copy_geotags failed with exit code $LASTEXITCODE" }
        Write-Host ""
        Write-Host "  wrote $edit" -ForegroundColor Green
    }
} finally {
    Remove-Item -LiteralPath $py -Force -ErrorAction SilentlyContinue
}
