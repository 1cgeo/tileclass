"""Point PROJ at rasterio's bundled proj.db before rasterio is imported.

Windows machines here ship up to 3 conflicting proj.db installs (PostgreSQL/
PostGIS, pyproj, rasterio) and a system-wide PROJ_LIB often points at the
PostGIS one, whose schema version rasterio's PROJ refuses ("Cannot find
proj.db"). Every module that touches rasterio CRS code calls
`configure_proj_data()` at import time, *before* `import rasterio`.

Rules (single source of truth — don't re-implement in callers):
  - locate rasterio via importlib (works in venvs, conda and system installs;
    `sys.executable`-relative paths break inside a venv's Scripts/ dir);
  - OVERRIDE PROJ_DATA/PROJ_LIB (setdefault would keep a poisoned value);
  - no-op when rasterio or its proj_data dir is missing (e.g. Linux wheels
    built against a system PROJ).
"""
import importlib.util
import os
from pathlib import Path


def rasterio_proj_dir() -> Path | None:
    spec = importlib.util.find_spec("rasterio")
    if not spec or not spec.origin:
        return None
    d = Path(spec.origin).parent / "proj_data"
    return d if (d / "proj.db").exists() else None


def configure_proj_data() -> Path | None:
    """Set PROJ_DATA/PROJ_LIB to rasterio's proj_data. Returns the directory
    used, or None when nothing was changed."""
    d = rasterio_proj_dir()
    if d is not None:
        os.environ["PROJ_DATA"] = str(d)
        os.environ["PROJ_LIB"] = str(d)
    return d
