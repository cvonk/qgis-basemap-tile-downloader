# Re-sync every QGIS plugin under this folder into the QGIS default-profile
# plugins folder.
#
# A plugin package is a folder that contains a metadata.txt, located either:
#   - directly under this folder (flat layout, e.g. xyz_aoi_downloader\), or
#   - one level inside a repo folder (nested layout, e.g.
#     WMS-AOI-Downloader-for-QGIS\wms_aoi_downloader\).
#
# The package folder name becomes the installed plugin name, so it must be a
# valid Python identifier (QGIS loads a plugin via `import <foldername>`).
# QGIS 3 and QGIS 4 keep SEPARATE profile roots, so a plugin must be synced into
# each one that exists — syncing only QGIS3 silently leaves QGIS 4 on an old
# version. Roots that don't exist are skipped.
#
# It ALSO copies the standalone QGIS Processing scripts in tools/ into each
# profile's processing\scripts\ folder, so the Toolbox picks up repo edits on
# reload. Those are copied file-by-file (not mirrored), so any other scripts you
# keep in that folder are left untouched.
# Usage:  pwsh -File sync.ps1     (or right-click -> Run with PowerShell)
$pluginsRoots = @('QGIS3', 'QGIS4') |
    ForEach-Object { Join-Path $env:APPDATA "QGIS\$_\profiles\default\python\plugins" } |
    Where-Object { Test-Path (Split-Path $_ -Parent) }
if (-not $pluginsRoots) {
    Write-Warning "No QGIS default profile found under $env:APPDATA\QGIS"
    return
}

function Get-PluginPackages {
    param([string]$Root)
    $found = @()
    foreach ($d in Get-ChildItem -Path $Root -Directory) {
        if ($d.Name -eq '__pycache__') { continue }
        if (Test-Path (Join-Path $d.FullName 'metadata.txt')) {
            $found += $d                                   # flat plugin
        } else {
            foreach ($sub in (Get-ChildItem -Path $d.FullName -Directory -ErrorAction SilentlyContinue)) {
                if (Test-Path (Join-Path $sub.FullName 'metadata.txt')) {
                    $found += $sub                          # nested in a repo
                }
            }
        }
    }
    return $found
}

$packages = Get-PluginPackages -Root $PSScriptRoot
if (-not $packages) {
    Write-Warning "No plugin packages (metadata.txt) found under $PSScriptRoot"
    return
}

foreach ($pkg in $packages) {
    if ($pkg.Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        Write-Warning ("Skipping '{0}' - not a valid QGIS plugin folder name (Python identifier)." -f $pkg.Name)
        continue
    }
    foreach ($root in $pluginsRoots) {
        $dst = Join-Path $root $pkg.Name
        robocopy $pkg.FullName $dst /MIR /XD __pycache__ .git /XF *.pyc /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        if ($LASTEXITCODE -lt 8) { Write-Host ("Synced {0,-30} -> {1}" -f $pkg.Name, $dst) }
        else { Write-Error ("robocopy failed for {0} (code {1})" -f $pkg.Name, $LASTEXITCODE) }
    }
}

# Standalone Processing scripts (tools/*.py) -> each profile's processing\scripts\.
# Copied per file (not mirrored) so other scripts in that folder are preserved.
$toolsDir = Join-Path $PSScriptRoot 'tools'
$toolScripts = if (Test-Path $toolsDir) { Get-ChildItem -Path $toolsDir -Filter *.py -File } else { @() }
if ($toolScripts) {
    $scriptsRoots = @('QGIS3', 'QGIS4') |
        ForEach-Object { Join-Path $env:APPDATA "QGIS\$_\profiles\default\processing\scripts" } |
        Where-Object { Test-Path (Split-Path (Split-Path $_ -Parent) -Parent) }   # profile exists
    foreach ($sroot in $scriptsRoots) {
        if (-not (Test-Path $sroot)) { New-Item -ItemType Directory -Path $sroot -Force | Out-Null }
        foreach ($s in $toolScripts) {
            $sdst = Join-Path $sroot $s.Name
            Copy-Item -Path $s.FullName -Destination $sdst -Force
            Write-Host ("Synced {0,-30} -> {1}" -f $s.Name, $sdst)
        }
    }
}
$global:LASTEXITCODE = 0
