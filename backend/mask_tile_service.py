"""Render colorized mask overlays in Web Mercator and cache them as MBTiles.

`get_tile(z, x, y)` returns a 256x256 RGBA PNG composed from every TileClass
tile whose bbox intersects the requested Web Mercator tile. Each pixel is
colored by its class id from `config.classes`. Used by the admin map view
to visualize all classifications at once at any zoom level.

Cache: a separate SQLite file in standard MBTiles schema (TMS y-axis on
disk, XYZ at the API boundary). Configurable via `mask_overlay.cache_path`.
Invalidation is bbox-driven: when a tile's mask or visible status changes,
every cached overlay tile that overlaps its footprint (with a 1-tile
margin to absorb reprojection edge effects) is deleted.
"""
import io
import math
import os
import sqlite3
import sys
import threading
from pathlib import Path

# PROJ fix (Windows has up to 3 conflicting proj.db installs from
# PostgreSQL/PostGIS, pyproj and rasterio). Point PROJ_DATA at rasterio's
# bundled directory BEFORE importing anything that touches CRS, otherwise
# `rasterio.crs.CRS.from_epsg(4326)` fails with a database-version mismatch
# at request time.
#
# Detection via `importlib.util.find_spec` so we resolve rasterio's actual
# install path in any environment (system Python, venv, conda) — the older
# `Path(sys.executable).parent / "Lib" / "site-packages"` heuristic was
# wrong on Windows venvs (`<venv>\Scripts\python.exe` → `Scripts\Lib\…`).
# Override (not setdefault): if the user has a system-wide PROJ_LIB pointing
# at an old proj.db (PostgreSQL/PostGIS ships one), setdefault leaves the
# poisoned value and every reproject 500s.
import importlib.util as _iu
_spec = _iu.find_spec("rasterio")
if _spec and _spec.origin:
    _RASTERIO_PROJ = Path(_spec.origin).parent / "proj_data"
    if _RASTERIO_PROJ.exists():
        os.environ["PROJ_DATA"] = str(_RASTERIO_PROJ)
        os.environ["PROJ_LIB"] = str(_RASTERIO_PROJ)

import numpy as np
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from .config import get_config
from .database import connect as connect_main
from .mask_utils import decode_mask, TILE_SIZE


# Web Mercator (EPSG:3857) earth half-circumference in meters.
_WM_HALF = 20037508.342789244

# Statuses whose data_png we render in the overlay. Excludes pending (no
# data yet), in_progress (incomplete draft), problem (mask wiped), blocked
# (out of distribution).
_VISIBLE_STATUSES = ("classified", "in_review", "reviewed")

_DEFAULT_MIN_Z = 8
_DEFAULT_MAX_Z = 18
_DEFAULT_CACHE_REL = "data/mask_overlay_cache.mbtiles"

# The cache path is resolved once per process from config; tests reset it
# via `reset_cache_path()` after monkey-patching the config.
_cache_path: Path | None = None
# Serialise schema-creating opens; SQLite handles concurrent writers itself
# but creating the cache file from two threads at once can race on mkdir.
_cache_lock = threading.Lock()


def _config_section() -> dict:
    return get_config().get("mask_overlay") or {}


def _resolve_cache_path() -> Path:
    global _cache_path
    if _cache_path is not None:
        return _cache_path
    raw = _config_section().get("cache_path") or _DEFAULT_CACHE_REL
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    _cache_path = p
    return p


def reset_cache_path() -> None:
    """Forget the cached path so the next call rereads config. Test hook."""
    global _cache_path
    _cache_path = None


def min_zoom() -> int:
    return int(_config_section().get("min_zoom", _DEFAULT_MIN_Z))


def max_zoom() -> int:
    return int(_config_section().get("max_zoom", _DEFAULT_MAX_Z))


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB,
    PRIMARY KEY (zoom_level, tile_column, tile_row)
);
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
"""


def _open_cache() -> sqlite3.Connection:
    with _cache_lock:
        path = _resolve_cache_path()
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.executescript(_CACHE_SCHEMA)
    return conn


# ---- Web Mercator math ----

def wm_tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) in EPSG:3857 meters for XYZ tile (z,x,y)."""
    n = 1 << z
    span = (2 * _WM_HALF) / n
    left = -_WM_HALF + span * x
    right = -_WM_HALF + span * (x + 1)
    top = _WM_HALF - span * y
    bottom = _WM_HALF - span * (y + 1)
    return (left, bottom, right, top)


def wm_tile_bounds_4326(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) in EPSG:4326 degrees for XYZ tile (z,x,y)."""
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    def _lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    north = _lat(y)
    south = _lat(y + 1)
    return (west, south, east, north)


def wm_tiles_for_bbox(z: int, west: float, south: float,
                      east: float, north: float) -> tuple[int, int, int, int]:
    """Inclusive XYZ tile range (x_min, y_min, x_max, y_max) covering bbox."""
    n = 1 << z
    def _lon_to_x(lon: float) -> int:
        return int(math.floor((lon + 180.0) / 360.0 * n))
    def _lat_to_y(lat: float) -> int:
        # Clamp to Web Mercator's valid latitude range.
        lat = max(min(lat, 85.05112878), -85.05112878)
        rad = math.radians(lat)
        return int(math.floor((1 - math.asinh(math.tan(rad)) / math.pi) / 2 * n))
    x_min = max(0, min(n - 1, _lon_to_x(west)))
    x_max = max(0, min(n - 1, _lon_to_x(east)))
    # XYZ y is inverted: north → small y, south → large y.
    y_min = max(0, min(n - 1, _lat_to_y(north)))
    y_max = max(0, min(n - 1, _lat_to_y(south)))
    return (x_min, y_min, x_max, y_max)


def _tms_row(z: int, y_xyz: int) -> int:
    """Convert XYZ y (top-to-bottom) to MBTiles tile_row (TMS, bottom-to-top)."""
    return (1 << z) - 1 - y_xyz


# ---- Rendering ----

def _lut() -> np.ndarray:
    """RGBA lookup: index 0..255 -> [R, G, B, A]. Only 1..6 are populated;
    the rest stays at [0,0,0,0] so unfilled (255) and out-of-range pixels
    render fully transparent."""
    lut = np.zeros((256, 4), dtype=np.uint8)
    for c in get_config()["classes"]:
        h = c["color"].lstrip("#")
        lut[c["id"]] = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    return lut


_TRANSPARENT_PNG: bytes | None = None


def _transparent_png() -> bytes:
    """A minimal fully-transparent 256x256 PNG, materialised lazily and
    reused for empty regions so we don't touch PIL on the hot path."""
    global _TRANSPARENT_PNG
    if _TRANSPARENT_PNG is None:
        img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _TRANSPARENT_PNG = buf.getvalue()
    return _TRANSPARENT_PNG


def _render_tile(z: int, x: int, y: int) -> bytes | None:
    """Build the PNG by reprojecting every intersecting TileClass mask.
    Returns None when no tile contributes any painted pixel — the cache
    stores this as NULL so empty regions don't waste disk."""
    west, south, east, north = wm_tile_bounds_4326(z, x, y)
    placeholders = ",".join("?" * len(_VISIBLE_STATUSES))
    conn = connect_main()
    try:
        rows = conn.execute(
            f"""SELECT bbox_west, bbox_south, bbox_east, bbox_north, data_png
                FROM tiles
                WHERE status IN ({placeholders})
                  AND data_png IS NOT NULL
                  AND bbox_west < ? AND bbox_east > ?
                  AND bbox_south < ? AND bbox_north > ?""",
            (*_VISIBLE_STATUSES, east, west, north, south),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    left, bottom, right, top = wm_tile_bounds_3857(z, x, y)
    dst_transform = from_bounds(left, bottom, right, top, TILE_SIZE, TILE_SIZE)
    acc = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
    has_data = False

    for r in rows:
        try:
            raw = decode_mask(r["data_png"])
        except (ValueError, OSError):
            continue
        src_arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE)
        # Skip tiles with no painted pixels — common right after assignment.
        if not np.any(src_arr != 255):
            continue
        src_transform = from_bounds(
            r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"],
            TILE_SIZE, TILE_SIZE,
        )
        dst = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
        # Nearest neighbour: classes are categorical; bilinear would invent
        # ids that don't exist in the LUT.
        reproject(
            source=src_arr,
            destination=dst,
            src_transform=src_transform,
            src_crs="EPSG:4326",
            src_nodata=255,
            dst_transform=dst_transform,
            dst_crs="EPSG:3857",
            dst_nodata=255,
            resampling=Resampling.nearest,
        )
        valid = dst != 255
        if valid.any():
            # Adjacent tiles share edges (gap < 1mm) so overlap should be at
            # most a 1-pixel seam; last-write-wins is fine there.
            acc[valid] = dst[valid]
            has_data = True

    if not has_data:
        return None

    rgba = _lut()[acc]
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---- Public API ----

def get_tile(z: int, x: int, y: int) -> bytes:
    """Return the PNG for (z,x,y). Renders + caches on miss; returns the
    transparent PNG when the cache says the tile has no data."""
    row_tms = _tms_row(z, y)
    conn = _open_cache()
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles "
            "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, row_tms),
        ).fetchone()
        if row is not None:
            return row[0] if row[0] is not None else _transparent_png()
        png = _render_tile(z, x, y)
        # Empty region cached as NULL so the existence of the row is the
        # "we already checked, nothing here" signal — avoids re-rendering.
        conn.execute(
            "INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?,?,?,?)",
            (z, x, row_tms, png),
        )
        return png if png is not None else _transparent_png()
    finally:
        conn.close()


def invalidate_bbox(west: float, south: float, east: float, north: float) -> int:
    """Delete cache rows whose footprint overlaps the bbox at any cached
    zoom level. Adds a 1-tile margin on each side to absorb reprojection
    edge effects (a 4326 tile reprojected to 3857 can touch neighbouring
    WM tiles by a fraction of a pixel)."""
    deleted = 0
    z_lo, z_hi = min_zoom(), max_zoom()
    conn = _open_cache()
    try:
        for z in range(z_lo, z_hi + 1):
            x_min, y_min, x_max, y_max = wm_tiles_for_bbox(z, west, south, east, north)
            x_min = max(0, x_min - 1)
            y_min = max(0, y_min - 1)
            x_max = min((1 << z) - 1, x_max + 1)
            y_max = min((1 << z) - 1, y_max + 1)
            tms_min = _tms_row(z, y_max)
            tms_max = _tms_row(z, y_min)
            cur = conn.execute(
                """DELETE FROM tiles WHERE zoom_level=?
                   AND tile_column BETWEEN ? AND ?
                   AND tile_row BETWEEN ? AND ?""",
                (z, x_min, x_max, tms_min, tms_max),
            )
            deleted += cur.rowcount or 0
    finally:
        conn.close()
    return deleted


def invalidate_tile(tile_id: int) -> int:
    """Look up the tile's bbox and invalidate everything overlapping it.
    Returns 0 silently if the tile no longer exists (e.g. just deleted) —
    the caller already invalidated by bbox in that case if it cared."""
    conn = connect_main()
    try:
        row = conn.execute(
            "SELECT bbox_west, bbox_south, bbox_east, bbox_north FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return 0
    return invalidate_bbox(
        row["bbox_west"], row["bbox_south"],
        row["bbox_east"], row["bbox_north"],
    )


def safe_invalidate_tile(tile_id: int) -> None:
    """Best-effort invalidation that never raises. Mutation paths in
    tile_service/admin_service call this after a successful commit; a cache
    failure must not undo or block a real mutation. Worst case: a stale
    overlay tile lingers until the next overlapping invalidation."""
    try:
        invalidate_tile(tile_id)
    except Exception:
        pass


def safe_invalidate_bbox(west: float, south: float, east: float, north: float) -> None:
    """Same contract as safe_invalidate_tile but for a known bbox (used by
    delete_tile, where the row is gone by the time we'd look it up)."""
    try:
        invalidate_bbox(west, south, east, north)
    except Exception:
        pass


def safe_invalidate_tiles(tile_ids: list[int]) -> None:
    """Bulk variant of safe_invalidate_tile: 1 main-DB read for the bboxes,
    then 1 cache connection for the whole loop, vs N opens. Bboxes are
    processed individually (no union — would over-invalidate when the batch
    spans disjoint regions)."""
    if not tile_ids:
        return
    try:
        main = connect_main()
        try:
            placeholders = ",".join("?" * len(tile_ids))
            rows = main.execute(
                f"SELECT bbox_west, bbox_south, bbox_east, bbox_north "
                f"FROM tiles WHERE id IN ({placeholders})",
                tile_ids,
            ).fetchall()
        finally:
            main.close()
        if not rows:
            return
        z_lo, z_hi = min_zoom(), max_zoom()
        cache = _open_cache()
        try:
            for r in rows:
                for z in range(z_lo, z_hi + 1):
                    x_min, y_min, x_max, y_max = wm_tiles_for_bbox(
                        z, r["bbox_west"], r["bbox_south"],
                        r["bbox_east"], r["bbox_north"],
                    )
                    x_min = max(0, x_min - 1)
                    y_min = max(0, y_min - 1)
                    x_max = min((1 << z) - 1, x_max + 1)
                    y_max = min((1 << z) - 1, y_max + 1)
                    tms_min = _tms_row(z, y_max)
                    tms_max = _tms_row(z, y_min)
                    cache.execute(
                        """DELETE FROM tiles WHERE zoom_level=?
                           AND tile_column BETWEEN ? AND ?
                           AND tile_row BETWEEN ? AND ?""",
                        (z, x_min, x_max, tms_min, tms_max),
                    )
        finally:
            cache.close()
    except Exception:
        pass


def clear_cache() -> int:
    """Wipe every cached tile. Useful for admin tooling and tests."""
    conn = _open_cache()
    try:
        cur = conn.execute("DELETE FROM tiles")
        return cur.rowcount or 0
    finally:
        conn.close()


def cache_stats() -> dict:
    """Return cache file metrics for the maintenance dashboard. `rendered` are
    rows whose render produced painted pixels; `empty` are rows we already
    visited and confirmed are blank (stored as NULL so future requests skip
    rendering)."""
    path = _resolve_cache_path()
    conn = _open_cache()
    try:
        rendered = conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE tile_data IS NOT NULL"
        ).fetchone()[0]
        empty = conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE tile_data IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    file_size = path.stat().st_size if path.exists() else 0
    return {
        "path": str(path),
        "file_size_bytes": file_size,
        "rendered_tiles": int(rendered),
        "empty_tiles": int(empty),
        "min_zoom": min_zoom(),
        "max_zoom": max_zoom(),
    }
