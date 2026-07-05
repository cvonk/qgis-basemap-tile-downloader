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
# Usage:  pwsh -File sync.ps1     (or right-click -> Run with PowerShell)
$pluginsRoot = Join-Path $env:APPDATA 'QGIS\QGIS3\profiles\default\python\plugins'

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
    $dst = Join-Path $pluginsRoot $pkg.Name
    robocopy $pkg.FullName $dst /MIR /XD __pycache__ .git /XF *.pyc /NFL /NDL /NJH /NJS /NC /NS | Out-Null
    if ($LASTEXITCODE -lt 8) { Write-Host ("Synced {0,-20} -> {1}" -f $pkg.Name, $dst) }
    else { Write-Error ("robocopy failed for {0} (code {1})" -f $pkg.Name, $LASTEXITCODE) }
}
$global:LASTEXITCODE = 0
