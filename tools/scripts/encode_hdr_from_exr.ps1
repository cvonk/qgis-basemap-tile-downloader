<#
.SYNOPSIS
    Encode a Blender OpenEXR frame sequence to an HDR10 (PQ / BT.2020) H.265 MP4.

.DESCRIPTION
    Wraps ffmpeg for the "render EXR in Blender, deliver HDR10" step of the terrain
    flythrough pipeline. Blender writes scene-linear EXR in *Linear Rec.709*; this
    converts to BT.2020 primaries and the SMPTE ST 2084 (PQ) transfer with zscale,
    then encodes 10-bit HEVC with the HDR10 metadata players need.

    Auto-detects the frame range, checks for gaps, and refuses to clobber an
    existing file unless -Force.

.PARAMETER InputDir
    Folder holding the numbered EXR frames (e.g. ...\Rendered\Seceda).

.PARAMETER Output
    Output .mp4. Defaults to "<InputDir leaf> - Rendered.mp4" beside InputDir.

.PARAMETER FrameRate
    Frames per second. MUST match the Blender scene (24 for this project).
    ffmpeg assumes 25 for image sequences if this is not passed, which silently
    plays the render 4% fast.

.PARAMETER StartNumber
    First frame number. Auto-detected from the folder when omitted.

.PARAMETER Crf
    x265 quality, lower is better. 18 is a good master; the x265 default of 28
    is too lossy to grade from.

.PARAMETER NominalPeak
    Nits that scene-linear 1.0 maps to under PQ. Default 203 = BT.2408 HDR
    Reference White, which is also what Blender's Rec.2100-PQ display uses - so
    the encode matches a Blender "Standard" view exactly. Raise for a brighter
    grade; 100 makes it ~1 stop darker than the viewport.

.PARAMETER MaxNits
    Mastering display peak luminance written into the HDR10 metadata.

.PARAMETER Preview
    Encode only this many frames, for a quick look before committing to the
    full sequence.

.EXAMPLE
    .\encode_hdr_from_exr.ps1 -InputDir "U:\...\Rendered\Seceda" -FrameRate 24

.EXAMPLE
    .\encode_hdr_from_exr.ps1 -InputDir ".\Seceda" -Preview 120 -Force
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string] $InputDir,
    [string] $Output,
    # Exact rational, so 23.976 stays 24000/1001 rather than a rounded decimal.
    # This scene is fps=24 / fps_base=1.001, i.e. 23.976 - NOT 24.
    [ValidatePattern('^\d+(\.\d+)?(/\d+)?$')]
    [string] $FrameRate     = '24000/1001',
    [int]    $StartNumber   = -1,
    [string] $Pattern       = '%04d.exr',
    [string] $FFmpeg,
    [int]    $Crf           = 18,
    [ValidateSet('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow')]
    [string] $Preset        = 'medium',
    # Left empty, the primaries are read from the EXR's colorInteropID tag, which
    # Blender writes ("lin_rec709_scene" / "lin_rec2020_scene"). ffmpeg ignores
    # that tag, so getting it wrong silently mis-converts the gamut.
    [string] $InputPrimaries = '',
    # 203 = ITU-R BT.2408 HDR Reference White, and what Blender's Rec.2100-PQ
    # display maps scene-linear 1.0 to. Verified: encoding at 203 matches a
    # Blender "Standard" view on a Rec.2100-PQ display to 0.003% of full scale;
    # at 100 it is 6.3% darker. Use 100 only if something downstream expects it.
    [int]    $NominalPeak   = 203,
    [int]    $MaxNits       = 1000,
    [double] $MinNits       = 0.0001,
    [int]    $MaxCll        = 1000,
    [int]    $MaxFall       = 400,
    [int]    $Preview       = 0,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- ffmpeg
if (-not $FFmpeg) {
    $cmd = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $FFmpeg = $cmd.Source
    } elseif (Test-Path -LiteralPath '.\ffmpeg.exe') {
        $FFmpeg = (Resolve-Path '.\ffmpeg.exe').Path
    } else {
        throw "ffmpeg.exe not found on PATH or in the current directory. Pass -FFmpeg <path>."
    }
}
if (-not (Test-Path -LiteralPath $FFmpeg)) { throw "ffmpeg not found at: $FFmpeg" }

# ---------------------------------------------------------------- frames
if (-not (Test-Path -LiteralPath $InputDir -PathType Container)) {
    throw "Input folder not found: $InputDir"
}
$InputDir = (Resolve-Path -LiteralPath $InputDir).Path
$frames = Get-ChildItem -LiteralPath $InputDir -Filter '*.exr' -File |
          Where-Object { $_.BaseName -match '^\d+$' } |
          Sort-Object { [int]$_.BaseName }
if (-not $frames) { throw "No numbered .exr frames in: $InputDir" }

$first = [int]$frames[0].BaseName
$last  = [int]$frames[-1].BaseName
if ($StartNumber -lt 0) { $StartNumber = $first }

$expected = $last - $StartNumber + 1
$present  = ($frames | Where-Object { [int]$_.BaseName -ge $StartNumber }).Count
if ($present -ne $expected) {
    Write-Warning ("Sequence has gaps: frames {0}-{1} is {2} frames but only {3} exist. " +
                   "ffmpeg stops at the first missing frame." -f $StartNumber, $last, $expected, $present)
}

$digits = $frames[0].BaseName.Length
if ($Pattern -eq '%04d.exr' -and $digits -ne 4) {
    Write-Warning "Frames use $digits digits; -Pattern is '$Pattern'. Pass -Pattern '%0${digits}d.exr'."
}

# ---------------------------------------------------------------- colour space
# Blender 5.x stamps the working space into the EXR header as colorInteropID and
# reads it back itself, so a mismatch is invisible inside Blender - but ffmpeg
# does not read it, so we must.
function Get-ExrColorSpace([string] $Path) {
    $buf = New-Object byte[] 8192
    $fs  = [System.IO.File]::OpenRead($Path)
    try { $n = $fs.Read($buf, 0, $buf.Length) } finally { $fs.Dispose() }
    $txt = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
    $i   = $txt.IndexOf('colorInteropID')
    if ($i -lt 0) { return $null }
    $p = $i + 'colorInteropID'.Length + 1            # past the name and its NUL
    while ($p -lt $n -and $buf[$p] -ne 0) { $p++ }   # past the type string
    $p++
    if ($p + 4 -gt $n) { return $null }
    $size = [BitConverter]::ToInt32($buf, $p); $p += 4
    if ($size -le 0 -or $p + $size -gt $n) { return $null }
    return [System.Text.Encoding]::ASCII.GetString($buf, $p, $size).Trim([char]0)
}
$csToPrimaries = @{ 'lin_rec709_scene' = 'bt709'; 'lin_rec2020_scene' = 'bt2020' }

$sample = @($frames | Where-Object { [int]$_.BaseName -ge $StartNumber })
$step   = [Math]::Max(1, [int]($sample.Count / 8))
$probe  = @(for ($k = 0; $k -lt $sample.Count; $k += $step) { $sample[$k] }) + @($sample[-1])
# @(...) matters: on a scalar string, $tags[0] would return its first character
$tags   = @($probe | ForEach-Object { Get-ExrColorSpace $_.FullName } |
            Where-Object { $_ } | Select-Object -Unique)

if ($tags.Count -gt 1) {
    throw "Frames are not all in the same colour space: $($tags -join ', '). " +
          "Re-render the odd ones, or encode the two runs separately."
}
$tag      = if ($tags.Count -eq 1) { $tags[0] } else { $null }
$detected = if ($tag) { $csToPrimaries[$tag] } else { $null }

if (-not $InputPrimaries) {
    if ($detected) {
        $InputPrimaries = $detected
    } else {
        $InputPrimaries = 'bt709'
        $why = if ($tag) { " (unmapped tag '$tag')" } else { " (no colorInteropID)" }
        Write-Warning "Could not determine the EXR colour space$why; assuming bt709. Pass -InputPrimaries to be sure."
    }
} elseif ($detected -and $detected -ne $InputPrimaries) {
    Write-Warning "-InputPrimaries $InputPrimaries, but the frames are tagged '$tag' (= $detected). Using your value - the gamut will be wrong if that was a slip."
}

if (-not $Output) {
    $leaf   = Split-Path -Leaf $InputDir
    $Output = Join-Path (Split-Path -Parent $InputDir) "$leaf - Rendered.mp4"
}
if ((Test-Path -LiteralPath $Output) -and -not $Force -and -not $WhatIfPreference) {
    throw "Output already exists: $Output`nPass -Force to overwrite."
}

# ---------------------------------------------------------------- filter + encoder
# transferin=linear tells zscale the EXR is scene-linear; primariesin must match
# what Blender wrote (Linear Rec.709), or the gamut conversion is a no-op and
# colours come out oversaturated.
$vf = @(
    'zscale=transferin=linear'
    "primariesin=$InputPrimaries"
    'transfer=smpte2084'
    'primaries=bt2020'
    'matrix=bt2020nc'
    'range=limited'
    "npl=$NominalPeak"
) -join ':'

# mastering display: BT.2020 primaries, in x265's 0.00002 / 0.0001 units
$L = '{0},{1}' -f [long]($MaxNits * 10000), [long]($MinNits * 10000)
$masterDisplay = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L($L)"
$x265 = @(
    'colorprim=bt2020'
    'transfer=smpte2084'
    'colormatrix=bt2020nc'
    'hdr10=1'
    'hdr10-opt=1'
    'repeat-headers=1'
    "master-display=$masterDisplay"
    "max-cll=$MaxCll,$MaxFall"
) -join ':'

$ffArgs = @(
    '-hide_banner'
    '-y'
    '-framerate', $FrameRate          # before -i: the INPUT rate. Omit and ffmpeg assumes 25.
    '-start_number', $StartNumber
    '-i', (Join-Path $InputDir $Pattern)
)
if ($Preview -gt 0) { $ffArgs += @('-frames:v', $Preview) }
$ffArgs += @(
    '-vf', $vf
    '-pix_fmt', 'yuv420p10le'
    '-c:v', 'libx265'
    '-preset', $Preset
    '-crf', $Crf
    '-tag:v', 'hvc1'
    '-x265-params', $x265
    '-color_primaries', 'bt2020'
    '-color_trc', 'smpte2084'
    '-colorspace', 'bt2020nc'
    '-color_range', 'tv'
    '-movflags', '+write_colr'
    $Output
)

$count = if ($Preview -gt 0) { [Math]::Min($Preview, $present) } else { $present }
$fpsNum = if ($FrameRate -match '^(\d+(?:\.\d+)?)/(\d+)$') {
    [double]$Matches[1] / [double]$Matches[2]
} else { [double]$FrameRate }
Write-Host ""
Write-Host "  source    $InputDir\$Pattern"
Write-Host "  frames    $StartNumber-$last  ($count frames @ $FrameRate fps = $([Math]::Round($count / $fpsNum, 1)) s)"
Write-Host "  grade     linear/$InputPrimaries -> PQ/BT.2020, diffuse white $NominalPeak nits"
Write-Host "  encode    libx265 crf $Crf $Preset, 10-bit, HDR10 @ $MaxNits nits"
Write-Host "  output    $Output"
Write-Host ""

if (-not $PSCmdlet.ShouldProcess($Output, "encode $count frames")) {
    Write-Host "ffmpeg $($ffArgs -join ' ')"
    return
}

& $FFmpeg @ffArgs
if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed with exit code $LASTEXITCODE" }

$size = (Get-Item -LiteralPath $Output).Length / 1MB
Write-Host ""
Write-Host ("  done      {0}  ({1:N1} MB)" -f $Output, $size) -ForegroundColor Green
Write-Host "  verify    ffprobe -show_streams `"$Output`" | Select-String 'color_|transfer'"
