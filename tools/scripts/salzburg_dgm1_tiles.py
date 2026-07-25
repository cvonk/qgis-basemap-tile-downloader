import os, csv

BASE = "https://service.salzburg.gv.at/sagisogd/archiv/raster/hoehen/laserscan/Rasterpunkte/DGM/DGM_1"
IDS = """
43255000 43255001 43255002 43255003 43255100 43255101 43255102 43255103 43255200 43255201 43255300 43255301
43265000 43265001 43265002 43265003 43265100 43265101 43265102 43265103 43265200 43265201 43265202 43265203
43265300 43265301 43265302 43265303 44255000 44255001 44255002 44255003 44255100 44255101 44255102 44255103
44255200 44255201 44255300 44255301 44265000 44265001 44265002 44265003 44265100 44265101 44265102 44265103
44265200 44265201 44265202 44265203 44265300 44265301 44265302 44265303
""".split()

OUTDIR = os.path.dirname(os.path.abspath(__file__))
CSVPATH = os.path.join(OUTDIR, "salzburg_dgm1_tiles.csv")

with open(CSVPATH, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, delimiter=";")
    w.writerow(["sheet_id", "filename", "url"])
    for sid in IDS:
        fn = f"{sid}_dgm_rp_1_m.tif"
        w.writerow([sid, fn, f"{BASE}/{fn}"])

print(f"wrote {len(IDS)} rows -> {CSVPATH}")
