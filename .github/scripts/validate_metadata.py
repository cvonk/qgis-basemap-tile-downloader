#!/usr/bin/env python3
"""Validate the plugin's metadata.txt has the fields QGIS requires."""
import configparser
import sys

PATH = "basemap_tile_downloader/metadata.txt"
REQUIRED = ["name", "qgisMinimumVersion", "description",
            "version", "author", "email"]

cp = configparser.ConfigParser(interpolation=None)   # tolerate % in values
if not cp.read(PATH, encoding="utf-8"):
    sys.exit(f"ERROR: could not read {PATH}")
if not cp.has_section("general"):
    sys.exit(f"ERROR: {PATH} has no [general] section")

missing = [k for k in REQUIRED if not cp.get("general", k, fallback="").strip()]
if missing:
    sys.exit(f"ERROR: metadata.txt missing required field(s): {', '.join(missing)}")

print(f"metadata.txt OK — {cp.get('general', 'name')} "
      f"v{cp.get('general', 'version')}")
