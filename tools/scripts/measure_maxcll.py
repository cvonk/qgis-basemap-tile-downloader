"""Measure MaxCLL / MaxFALL of a scene-linear EXR sequence, for HDR10 metadata.

Per CTA-861.3:
  MaxCLL  = the largest max(R,G,B) of any pixel in the whole sequence, in nits
  MaxFALL = the largest per-frame mean of max(R,G,B), in nits

x265's default `max-cll=1000,400` is a guess. Declaring a peak far above what the
content actually reaches invites displays to tone-map more than they need to, so
it is worth measuring once per sequence and passing the real numbers to
encode_hdr_from_exr.ps1 via -MaxCll / -MaxFall.

ffmpeg converts the frames to PQ for us (the same chain the encoder uses), so the
values arrive display-referred in 0..1 covering 0..10000 nits; we invert PQ to get
nits. --npl must match the encode, or the numbers describe a different grade.

Needs numpy. Blender ships one:
  "…/Blender 5.2/5.2/python/bin/python.exe" measure_maxcll.py …

Example:
  python measure_maxcll.py --input "…/Rendered/Seceda/%04d.exr" --start 50 \
      --primaries bt2020 --npl 203 --ffmpeg "…/ffmpeg.exe"
"""
import argparse
import json
import subprocess
import sys

import numpy as np


def pq_to_nits(e):
    """Invert SMPTE ST 2084. e in 0..1 -> cd/m^2."""
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    ep = np.power(np.clip(e, 0.0, 1.0), 1.0 / m2)
    num = np.maximum(ep - c1, 0.0)
    den = np.maximum(c2 - c3 * ep, 1e-9)
    return 10000.0 * np.power(num / den, 1.0 / m1)


def probe_size(ffmpeg, pattern, start):
    """Ask ffprobe for the frame size, so the caller need not pass it."""
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    cmd = [ffprobe, "-v", "error", "-start_number", str(start), "-i", pattern,
           "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        s = json.loads(out)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception as exc:                                    # noqa: BLE001
        sys.exit(f"could not probe frame size ({exc}); pass --size WxH")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help=r"printf pattern, e.g. …\%%04d.exr")
    ap.add_argument("--start", type=int, default=1, help="first frame number")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg.exe")
    ap.add_argument("--npl", type=int, default=203,
                    help="nits that scene-linear 1.0 maps to; must match the encode")
    ap.add_argument("--primaries", default="bt2020",
                    help="primaries the EXRs are in (bt709 / bt2020)")
    ap.add_argument("--size", help="WxH, if ffprobe cannot work it out")
    a = ap.parse_args()

    if a.size:
        w, h = (int(v) for v in a.size.lower().split("x"))
    else:
        w, h = probe_size(a.ffmpeg, a.input, a.start)

    vf = (f"zscale=transferin=linear:primariesin={a.primaries}:transfer=smpte2084:"
          f"primaries=bt2020:npl={a.npl}:range=full")
    cmd = [a.ffmpeg, "-hide_banner", "-loglevel", "error", "-start_number", str(a.start),
           "-i", a.input, "-vf", vf, "-pix_fmt", "rgb48le", "-f", "rawvideo", "-"]

    nbytes = w * h * 3 * 2
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=nbytes)
    maxcll = maxfall = 0.0
    peak_frame = fall_frame = -1
    n = 0
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        px = np.frombuffer(buf, dtype="<u2").reshape(h, w, 3).astype(np.float32) / 65535.0
        nits = pq_to_nits(px.max(axis=2))            # per-pixel max(R,G,B)
        fmax, favg = float(nits.max()), float(nits.mean())
        if fmax > maxcll:
            maxcll, peak_frame = fmax, a.start + n
        if favg > maxfall:
            maxfall, fall_frame = favg, a.start + n
        n += 1
        if n % 100 == 0:
            print(f"  {n:5d} frames   MaxCLL={maxcll:7.1f}  MaxFALL={maxfall:6.1f}", flush=True)
    proc.stdout.close()
    proc.wait()

    if not n:
        sys.exit("no frames decoded - check --input, --start and the ffmpeg path")
    print(f"\nframes analysed : {n}  ({w}x{h}, npl {a.npl}, primariesin {a.primaries})")
    print(f"MaxCLL          : {maxcll:.1f} nits   (frame {peak_frame})")
    print(f"MaxFALL         : {maxfall:.1f} nits   (frame {fall_frame})")
    print(f"\n-> encode_hdr_from_exr.ps1 -MaxCll {round(maxcll)} -MaxFall {round(maxfall)}")


if __name__ == "__main__":
    main()
