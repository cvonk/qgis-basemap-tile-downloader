# Present so pytest adds the repo root to sys.path, making
# `import basemap_tile_downloader` work. basemap_tile_downloader/__init__.py
# imports nothing at module load (classFactory imports QGIS lazily), and
# basemap_tile_downloader/tilemath.py is pure Python, so the tilemath tests run
# without a QGIS install.
