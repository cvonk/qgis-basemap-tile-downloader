# -*- coding: utf-8 -*-
"""Measure the sun azimuth/elevation an orthophoto was flown under, per AOI.

The Blender sun has to match the shadows already baked into the ortho, or the
render's lighting fights its own texture. This recovers that sun position from
the ortho plus its DTM, and prints the values to set in Blender.

WHY NOT HILLSHADE CORRELATION
  The obvious method - correlate an analytic hillshade against ortho luminance -
  was tried and fails here. On Seceda (expected ~190/60) it returned az 146-148,
  el 17-37, and its elevation score was near-flat across 21-53 deg. The reason is
  physical: in the Alps ASPECT CORRELATES WITH ALBEDO (north faces hold snow and
  forest, south faces are grass and rock), so the fit locks onto ground cover
  instead of illumination. It is kept below only as a reported cross-check.

WHAT THIS DOES INSTEAD - cast-shadow matching
  Cast shadows are geometric: hard edges tied to sun direction, and length that
  scales with cot(elevation), which is exactly the constraint diffuse shading
  lacks. For a candidate azimuth we march each pixel toward the sun and keep the
  largest angle subtended by upwind terrain (its "horizon angle"). Then

      pixel is in shadow  <=>  horizon_angle > sun_elevation

  so ONE scan per azimuth scores EVERY elevation, just by thresholding.

  The observed shadow mask is taken as the darkest pixels of the ortho - but at
  the SAME prevalence as the prediction. Fixing an arbitrary "shadows are 15% of
  the image" would bias the fit toward low sun angles, which produce more shadow;
  matching prevalence removes that bias and turns the score into pure spatial
  agreement. Score is then chance-corrected overlap (Cohen's kappa at matched
  prevalence): (hit_rate - f) / (1 - f), where f is the shadow fraction.

Needs the GDAL Python bindings - run it with the QGIS Python:
  "U:\\Program Files\\QGIS 3.44.11\\bin\\python-qgis-ltr.bat" measure_sun_from_ortho.py --list
  ... measure_sun_from_ortho.py --synthetic 190,60      # correctness gate
  ... measure_sun_from_ortho.py --only Seceda
  ... measure_sun_from_ortho.py --csv sun_positions.csv
"""
import argparse
import csv
import math
import os
import re
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

DEFAULT_ROOT = (r"U:\Users\Family\Videos\Editing\sources\Johan en Sander Vonk - s23"
                r"\Johan en Sander Vonk - s23e02\Graphics\3D\QGIS\Area")
NO_BLOCK = -1e9                      # fill value that can never cast a shadow


# ----------------------------------------------------------------- discovery
def parse_name(fname):
    """'<AOI> - <rest> (<res> <epsg> <extent>).tif' -> (aoi, rest, epsg, extent)."""
    stem = os.path.splitext(fname)[0]
    if " - " not in stem:
        return None
    aoi, rest = stem.split(" - ", 1)             # same convention as crop_aois.py
    m = re.search(r"\(([^)]*)\)\s*$", rest)
    toks = m.group(1).split() if m else []
    rest = rest[:m.start()].strip() if m else rest.strip()
    epsg = next((t for t in toks if re.fullmatch(r"\d{4,5}", t)), "")
    extent = next((t for t in toks if re.fullmatch(r"[\d.]+k", t)), "")
    return aoi.strip(), rest, epsg, extent


def source_of(rest):
    """Ortho source with the processing words removed: 'Nazionale', 'Copernicus'..."""
    s = re.sub(r"\b(matched|stretched|part)\b", " ", rest, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def rank_variant(rest):
    """Prefer the least-processed variant of the same photo."""
    return ("stretched" in rest.lower()) + ("matched" in rest.lower())


def discover(root, only=None):
    """One (dtm, ortho) pair per AOI x extent x ortho source."""
    pairs = []
    for aoi_dir in sorted(d for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d))):
        d = os.path.join(root, aoi_dir)
        # top level ONLY - this is what skips Orthofoto Scratch / _backup_precrop
        files = [f for f in os.listdir(d)
                 if f.lower().endswith((".tif", ".tiff"))
                 and os.path.isfile(os.path.join(d, f))]
        dtms, orthos = {}, {}
        for f in files:
            p = parse_name(f)
            if not p:
                continue
            aoi, rest, epsg, extent = p
            key = (epsg, extent)
            if rest.upper().startswith(("DTM", "DEM")):   # DEM = a DTM with nodata filled
                dtms[key] = os.path.join(d, f)
            else:
                k = key + (source_of(rest),)
                if k not in orthos or rank_variant(rest) < rank_variant(orthos[k][1]):
                    orthos[k] = (os.path.join(d, f), rest)
        for (epsg, extent, src), (path, _rest) in sorted(orthos.items()):
            dtm = dtms.get((epsg, extent))
            if not dtm:
                continue
            if only and only.lower() not in (aoi_dir + " " + src).lower():
                continue
            pairs.append({"aoi": aoi_dir, "source": src, "epsg": epsg,
                          "extent": extent, "dtm": dtm, "ortho": path})
    return pairs


# --------------------------------------------------------------------- raster
def read_band(path, band, n):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(band)
    a = b.ReadAsArray(buf_xsize=n, buf_ysize=n).astype(np.float32)
    nod = b.GetNoDataValue()
    if nod is not None:
        a[a == nod] = np.nan
    return a, abs(ds.GetGeoTransform()[1]) * ds.RasterXSize / n


def read_lum(path, n):
    ds = gdal.Open(path)
    nb = min(3, ds.RasterCount)
    bands = [read_band(path, i + 1, n)[0] for i in range(nb)]
    if nb >= 3:
        return 0.2126 * bands[0] + 0.7152 * bands[1] + 0.0722 * bands[2]
    return bands[0]


def shift2d(a, dr, dc, fill):
    """out[i, j] = a[i + dr, j + dc], padded with `fill` (no wraparound)."""
    h, w = a.shape
    out = np.full_like(a, fill)
    i0, i1 = max(0, -dr), min(h, h - dr)
    j0, j1 = max(0, -dc), min(w, w - dc)
    if i0 < i1 and j0 < j1:
        out[i0:i1, j0:j1] = a[i0 + dr:i1 + dr, j0 + dc:j1 + dc]
    return out


def step_ladder(kmax, growth=1.12):
    """1, 2, 3, ... growing geometrically - near terrain matters most."""
    ks, k = [], 1.0
    while k <= kmax:
        ks.append(int(round(k)))
        k *= growth
    return sorted(set(ks))


def horizon_tan(dem, cell, az_deg, kmax):
    """Largest tan(angle) to terrain in the direction of the sun, per pixel."""
    a = math.radians(az_deg)
    dcol, drow = math.sin(a), -math.cos(a)          # east = +col, north = -row
    best = np.full(dem.shape, -np.inf, dtype=np.float32)
    for k in step_ladder(kmax):
        dr, dc = int(round(k * drow)), int(round(k * dcol))
        if dr == 0 and dc == 0:
            continue
        dist = math.hypot(dr, dc) * cell
        np.maximum(best, (shift2d(dem, dr, dc, NO_BLOCK) - dem) / dist, out=best)
    return best


# ---------------------------------------------------------------------- score
def score_azimuth(htan, lum_rank, valid, elevations):
    """Chance-corrected overlap for each elevation, at matched prevalence."""
    out = []
    nvalid = int(valid.sum())
    for el in elevations:
        pred = (htan > math.tan(math.radians(el))) & valid
        f = pred.sum() / nvalid
        if not 0.005 < f < 0.5:                     # degenerate: skip
            out.append((-1.0, el, f))
            continue
        hit = float((pred & (lum_rank < f)).sum()) / max(pred.sum(), 1)
        out.append(((hit - f) / (1.0 - f), el, f))
    return out


def hillshade_r(dem, cell, lum, valid, az, el):
    """Plain hillshade correlation - reported as a cross-check only."""
    gy, gx = np.gradient(dem, cell)
    nx, ny, nz = -gx, gy, np.ones_like(gx)
    n = np.sqrt(nx * nx + ny * ny + nz * nz)
    ar, er = math.radians(az), math.radians(el)
    s = (math.cos(er) * math.sin(ar), math.cos(er) * math.cos(ar), math.sin(er))
    h = np.clip((nx * s[0] + ny * s[1] + nz * s[2]) / n, 0, None)[valid]
    v = lum[valid]
    if h.std() < 1e-9 or v.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(h, v)[0, 1])


def estimate(dem, cell, lum, size, coarse=10, verbose=True):
    """-> dict with azimuth, elevation, score, margin, interior."""
    valid = np.isfinite(dem) & np.isfinite(lum)
    dem = np.where(np.isfinite(dem), dem, np.nanmin(dem[np.isfinite(dem)])).astype(np.float32)
    # rank-transform luminance once: "darkest f fraction" becomes "rank < f"
    lum_rank = np.full(lum.shape, 2.0, dtype=np.float32)
    v = lum[valid]
    order = np.argsort(v, kind="stable")
    r = np.empty(v.shape, dtype=np.float32)
    r[order] = np.arange(v.size, dtype=np.float32) / v.size
    lum_rank[valid] = r

    kmax = int(size * 0.35)                         # longest shadow we look for
    els = list(range(6, 86, 2))

    def sweep(azimuths, els_):
        res = []
        for az in azimuths:
            ht = horizon_tan(dem, cell, az, kmax)
            for sc, el, f in score_azimuth(ht, lum_rank, valid, els_):
                res.append((sc, az, el, f))
            if verbose:
                print(".", end="", flush=True)
        return res

    grid = sweep(range(0, 360, coarse), els)
    grid.sort(reverse=True)
    _, baz, bel, _ = grid[0]

    fine = sweep([a % 360 for a in range(baz - coarse, baz + coarse + 1, 2)],
                 [e for e in range(bel - 6, bel + 7, 1) if 1 <= e <= 89])
    fine.sort(reverse=True)
    sc, az, el, f = fine[0]

    # how much better than the best genuinely different azimuth?
    away = [x for x in grid if min(abs(x[1] - az), 360 - abs(x[1] - az)) >= 25]
    margin = sc - (away[0][0] if away else 0.0)
    interior = 6 < el < 84
    return {"azimuth": az % 360, "elevation": el, "score": sc, "margin": margin,
            "shadow_frac": f, "interior": interior,
            "hillshade_r": hillshade_r(dem, cell, lum, valid, az, el)}


def verdict(res):
    """Be strict: two earlier methods failed by printing a confident wrong number.

    The characteristic runaway is a fit sliding to a LOW sun, because a low sun
    predicts shadow over every north-facing slope - and north-facing slopes are
    genuinely dark (forest, shade), so albedo pays the fit for being wrong. A
    shadow fraction over ~35% is that runaway, not a measurement. Real survey
    orthos are flown with a high sun and sit around 5-20%.
    """
    if not res["interior"] or res["shadow_frac"] > 0.35:
        return "UNRELIABLE"
    if res["score"] < 0.25 or res["margin"] < 0.05:
        return "WEAK"
    return "OK"


# ------------------------------------------------------------------ synthetic
def synthetic_lum(dem, cell, size, az, el):
    """A fake 'ortho': hillshade darkened where the terrain casts a shadow."""
    gy, gx = np.gradient(dem, cell)
    nx, ny, nz = -gx, gy, np.ones_like(gx)
    n = np.sqrt(nx * nx + ny * ny + nz * nz)
    ar, er = math.radians(az), math.radians(el)
    s = (math.cos(er) * math.sin(ar), math.cos(er) * math.cos(ar), math.sin(er))
    shade = np.clip((nx * s[0] + ny * s[1] + nz * s[2]) / n, 0, None)
    shadowed = horizon_tan(dem, cell, az, int(size * 0.35)) > math.tan(er)
    return (shade * np.where(shadowed, 0.15, 1.0)).astype(np.float32)


# ----------------------------------------------------------------------- main
def blender_values(az, el):
    """Lamp rotation flips by 180, the Sky Texture does NOT - see the note below.

    The lamp's rotation aims the light's TRAVEL direction (its local -Z), i.e.
    away from the sun, hence 180 - azimuth. sun_rotation is the direction the sun
    IS in, so it takes the azimuth unchanged. Measured with an equirectangular
    probe render (markers at +Y/+X to calibrate the mapping): sun_rotation -25
    put the disc at azimuth 335, sun_rotation 155 put it at 155. Using
    azimuth - 180 here silently placed the sky's sun opposite the lamp.
    """
    return {"sun_rot_x": round(90.0 - el, 1), "sun_rot_z": round(180.0 - az, 1),
            "sky_elevation": round(float(el), 1), "sky_rotation": round(float(az), 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--only", help="substring filter on '<AOI> <source>'")
    ap.add_argument("--list", action="store_true", help="show the pairs and exit")
    ap.add_argument("--size", type=int, default=600, help="working grid (px per side)")
    ap.add_argument("--csv", help="write results here")
    ap.add_argument("--synthetic", help="AZ,EL - self-test on a rendered DTM, no ortho")
    a = ap.parse_args()

    pairs = discover(a.root, a.only)
    if not pairs:
        sys.exit("no DTM/ortho pairs found under %r" % a.root)
    if a.list:
        for p in pairs:
            print("%-22s %-22s %-6s %-6s" % (p["aoi"], p["source"], p["epsg"], p["extent"]))
        print("\n%d pairs" % len(pairs))
        return

    if a.synthetic:
        taz, tel = (float(x) for x in a.synthetic.split(","))
        p = pairs[0]
        print("GATE: synthetic recovery on %s  (truth az %.0f / el %.0f)"
              % (p["aoi"], taz, tel))
        dem, cell = read_band(p["dtm"], 1, a.size)
        dem[dem < -1000] = np.nan
        lum = synthetic_lum(np.nan_to_num(dem, nan=float(np.nanmin(dem))),
                            cell, a.size, taz, tel)
        res = estimate(dem, cell, lum, a.size)
        daz = min(abs(res["azimuth"] - taz), 360 - abs(res["azimuth"] - taz))
        dele = abs(res["elevation"] - tel)
        ok = daz <= 5 and dele <= 5
        print("\n  recovered az %3d / el %2d   score %.3f   err az %.0f / el %.0f  -> %s"
              % (res["azimuth"], res["elevation"], res["score"], daz, dele,
                 "PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 1)

    rows = []
    for p in pairs:
        print("\n%-22s %-22s " % (p["aoi"], p["source"]), end="", flush=True)
        dem, cell = read_band(p["dtm"], 1, a.size)
        dem[dem < -1000] = np.nan
        lum = read_lum(p["ortho"], a.size)
        res = estimate(dem, cell, lum, a.size)
        v = verdict(res)
        bl = blender_values(res["azimuth"], res["elevation"])
        print("\n   az %3d  el %2d   score %.3f  margin %.3f  shadow %4.1f%%  "
              "hillshade_r %+.2f  [%s]"
              % (res["azimuth"], res["elevation"], res["score"], res["margin"],
                 100 * res["shadow_frac"], res["hillshade_r"], v))
        print("   blender: Sun rotation X %.1f  Z %.1f   Sky elev %.1f  rot %.1f"
              % (bl["sun_rot_x"], bl["sun_rot_z"], bl["sky_elevation"], bl["sky_rotation"]))
        rows.append(dict(aoi=p["aoi"], source=p["source"], epsg=p["epsg"],
                         extent=p["extent"], azimuth=res["azimuth"],
                         elevation=res["elevation"], score=round(res["score"], 4),
                         margin=round(res["margin"], 4),
                         shadow_pct=round(100 * res["shadow_frac"], 1),
                         hillshade_r=round(res["hillshade_r"], 3), verdict=v, **bl))

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print("\nwrote %s" % a.csv)


if __name__ == "__main__":
    main()
