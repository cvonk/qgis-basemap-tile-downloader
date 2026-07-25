# Resize every AOI layer in the open project to a square about its current
# centre. Run from the QGIS Python Console (Plugins > Python Console) with ONE
# line (point it at wherever you keep this file):
#
#   exec(open(r"<path-to>/resize_aois.py", encoding="utf-8").read())
#
# Back up the vector AOI files first — shapefile edits commit to disk immediately.
# After it runs, save the project (Ctrl+S) so any memory-layer AOI persists too.
#
# Runs at import on purpose (so exec() executes it), so this file must NOT be
# placed in a QGIS processing/scripts/ folder — it would run on every launch.
from qgis.core import (QgsProject, QgsVectorLayer, Qgis,
                       QgsRectangle, QgsGeometry)

SIZE_EW = 16500.0            # West–East, metres (each layer's CRS units)
SIZE_NS = 16500.0            # North–South, metres
NAME_MUST_CONTAIN = "AOI"    # only layers whose name contains this

hx, hy = SIZE_EW / 2.0, SIZE_NS / 2.0
print(f"Resizing AOIs to {SIZE_EW:.0f} x {SIZE_NS:.0f} m about their centre:")
for lyr in QgsProject.instance().mapLayers().values():
    if not isinstance(lyr, QgsVectorLayer):
        continue
    if NAME_MUST_CONTAIN not in lyr.name():
        continue
    if lyr.geometryType() != Qgis.GeometryType.Polygon:
        continue
    try:
        feats = list(lyr.getFeatures())
        bb = None
        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            r = g.boundingBox()
            bb = QgsRectangle(r) if bb is None else bb
            bb.combineExtentWith(r)
        if bb is None or bb.isEmpty():
            print(f"  SKIP  {lyr.name()} (no geometry)")
            continue
        c = bb.center()
        square = QgsGeometry.fromRect(
            QgsRectangle(c.x() - hx, c.y() - hy, c.x() + hx, c.y() + hy))
        if not lyr.isEditable():
            lyr.startEditing()
        lyr.changeGeometry(feats[0].id(), square)
        if len(feats) > 1:               # keep exactly one square feature
            lyr.deleteFeatures([f.id() for f in feats[1:]])
        if lyr.commitChanges():
            print(f"  OK    {lyr.name():<34} centre ({c.x():.1f}, {c.y():.1f})  "
                  f"{bb.width():.0f}x{bb.height():.0f} -> "
                  f"{SIZE_EW:.0f}x{SIZE_NS:.0f} m")
        else:
            print(f"  FAIL  {lyr.name()}: {lyr.commitErrors()}")
    except Exception as e:
        print(f"  ERROR {lyr.name()}: {e}")
print("Done. Save the project (Ctrl+S) so the memory-layer AOI also persists.")
