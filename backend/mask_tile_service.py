"""Render colorized mask overlays in Web Mercator and cache them as MBTiles.

`get_tile(project_id, z, x, y)` returns a 256x256 RGBA PNG composed from every
TileClass tile in `project_id` whose bbox intersects (z,x,y). Each pixel is
colored by its class id from the project's `project_classes` palette. Used by
the admin map view to visualize a project's classifications at any zoom level.

Cache: a separate SQLite file per project (standard MBTiles schema; TMS y-axis
on disk, XYZ at the API boundary). File path is derived from
`mask_overlay.cache_path` in config: `<base>` becomes `<base>_p<project_id>.mbtiles`
so projects with different palettes never share cache rows.
"""
import io
import math
import os
import sqlite3
import threading
from pathlib import Path

# Windows ships up to 3 conflicting proj.db installs (PostgreSQL/PostGIS,
# pyproj, rasterio); point PROJ_DATA at rasterio's bundled directory before
# anything imports CRS code. Override (not setdefault): a poisoned system
# PROJ_LIB would otherwise break every reproject.
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

from . import project_service
from .config import get_config
from .database import connect as connect_main
from .mask_utils import decode_mask, TILE_SIZE


_WM_HALF = 20037508.342789244

# Statuses whose data_png we render. Excludes pending/in_progress (no useful
# mask), problem (mask wiped), and blocked (out of distribution).
_VISIBLE_STATUSES = ("classified", "in_review", "reviewed")

_DEFAULT_MIN_Z = 8
_DEFAULT_MAX_Z = 18
_DEFAULT_CACHE_REL = "data/mask_overlay_cache.mbtiles"

_cache_base: Path | None = None
_cache_lock = threading.Lock()


def _config_section() -> dict:
    return get_config().get("mask_overlay") or {}


def _resolve_cache_base() -> Path:
    """Resolve the cache base path once per process from config; tests reset
    it via `reset_cache_path()` after monkey-patching the config."""
    global _cache_base
    if _cache_base is not None:
        return _cache_base
    raw = _config_section().get("cache_path") or _DEFAULT_CACHE_REL
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    _cache_base = p
    return p


def cache_path(project_id: int) -> Path:
    """Per-project cache file: `<base>_p<id>.mbtiles`. Projects with
    different palettes/tilesets never share a cache row."""
    base = _resolve_cache_base()
    return base.with_name(f"{base.stem}_p{project_id}{base.suffix}")


def reset_cache_path() -> None:
    global _cache_base
    _cache_base = None


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


def _open_cache(project_id: int) -> sqlite3.Connection:
    with _cache_lock:
        path = cache_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # WAL + busy_timeout: the admin map fans out many parallel tile
        # requests, so the second writer must wait briefly instead of raising.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
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

def _lut(project_id: int) -> np.ndarray:
    """RGBA lookup keyed by class id, derived from the project's palette.
    Unfilled (255) and unmapped indexes stay at [0,0,0,0] so they render
    fully transparent."""
    proj = project_service.get_project(project_id)
    classes = (proj or {}).get("classes") or []
    lut = np.zeros((256, 4), dtype=np.uint8)
    for c in classes:
        h = c["color"].lstrip("#")
        lut[c["id"]] = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    return lut


_TRANSPARENT_PNG: bytes | None = None


def _transparent_png() -> bytes:
    global _TRANSPARENT_PNG
    if _TRANSPARENT_PNG is None:
        img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _TRANSPARENT_PNG = buf.getvalue()
    return _TRANSPARENT_PNG


def _render_tile(project_id: int, z: int, x: int, y: int) -> bytes | None:
    """Build the PNG by reprojecting every intersecting TileClass mask in the
    given project. Returns None when no tile contributes any painted pixel —
    the cache stores this as NULL so empty regions don't waste disk."""
    west, south, east, north = wm_tile_bounds_4326(z, x, y)
    placeholders = ",".join("?" * len(_VISIBLE_STATUSES))
    conn = connect_main()
    try:
        rows = conn.execute(
            f"""SELECT bbox_west, bbox_south, bbox_east, bbox_north, data_png
                FROM tiles
                WHERE project_id=?
                  AND status IN ({placeholders})
                  AND data_png IS NOT NULL
                  AND bbox_west < ? AND bbox_east > ?
                  AND bbox_south < ? AND bbox_north > ?""",
            (project_id, *_VISIBLE_STATUSES, east, west, north, south),
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
            # Adjacent tiles share edges (gap < 1mm) so overlap is at most a
            # 1-pixel seam; last-write-wins is fine.
            acc[valid] = dst[valid]
            has_data = True

    if not has_data:
        return None

    rgba = _lut(project_id)[acc]
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---- Public API ----

def get_tile(project_id: int, z: int, x: int, y: int) -> bytes:
    """PNG for (z,x,y) in `project_id`. Renders + caches on miss; returns the
    transparent PNG when the cache says the tile has no data."""
    row_tms = _tms_row(z, y)
    conn = _open_cache(project_id)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles "
            "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, row_tms),
        ).fetchone()
        if row is not None:
            return row[0] if row[0] is not None else _transparent_png()
        png = _render_tile(project_id, z, x, y)
        # Empty regions cached as NULL so the existence of the row is the
        # "we already checked, nothing here" signal — avoids re-rendering.
        conn.execute(
            "INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?,?,?,?)",
            (z, x, row_tms, png),
        )
        return png if png is not None else _transparent_png()
    finally:
        conn.close()


def _invalidate_bbox_in(conn: sqlite3.Connection, west: float, south: float,
                        east: float, north: float) -> int:
    """Delete cache rows in the given connection whose footprint overlaps the
    bbox at any cached zoom. 1-tile margin absorbs reprojection edge effects."""
    deleted = 0
    z_lo, z_hi = min_zoom(), max_zoom()
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
    return deleted


def invalidate_bbox(project_id: int, west: float, south: float,
                    east: float, north: float) -> int:
    conn = _open_cache(project_id)
    try:
        return _invalidate_bbox_in(conn, west, south, east, north)
    finally:
        conn.close()


def invalidate_tile(tile_id: int) -> int:
    """Look up the tile's project + bbox and invalidate everything overlapping.
    Returns 0 silently if the tile no longer exists."""
    conn = connect_main()
    try:
        row = conn.execute(
            "SELECT project_id, bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return 0
    return invalidate_bbox(
        row["project_id"],
        row["bbox_west"], row["bbox_south"],
        row["bbox_east"], row["bbox_north"],
    )


def safe_invalidate_tile(tile_id: int) -> None:
    """Best-effort invalidation that never raises. Mutation paths call this
    after a successful commit; a cache failure must not undo a real mutation."""
    try:
        invalidate_tile(tile_id)
    except Exception:
        pass


def safe_invalidate_bbox(project_id: int, west: float, south: float,
                         east: float, north: float) -> None:
    """Same contract as safe_invalidate_tile but for a known (project_id, bbox)
    — used by delete_tile, where the row is gone by the time we'd look it up."""
    try:
        invalidate_bbox(project_id, west, south, east, north)
    except Exception:
        pass


def safe_invalidate_tiles(tile_ids: list[int]) -> None:
    """Bulk variant: 1 main-DB read for (project_id, bbox), then 1 cache
    connection per project, vs N opens. Bboxes processed individually (no
    union — would over-invalidate when the batch spans disjoint regions)."""
    if not tile_ids:
        return
    try:
        main = connect_main()
        try:
            placeholders = ",".join("?" * len(tile_ids))
            rows = main.execute(
                f"SELECT project_id, bbox_west, bbox_south, bbox_east, bbox_north "
                f"FROM tiles WHERE id IN ({placeholders})",
                tile_ids,
            ).fetchall()
        finally:
            main.close()
        if not rows:
            return
        by_project: dict[int, list] = {}
        for r in rows:
            by_project.setdefault(r["project_id"], []).append(r)
        for pid, batch in by_project.items():
            cache = _open_cache(pid)
            try:
                for r in batch:
                    _invalidate_bbox_in(
                        cache,
                        r["bbox_west"], r["bbox_south"],
                        r["bbox_east"], r["bbox_north"],
                    )
            finally:
                cache.close()
    except Exception:
        pass


def _project_ids() -> list[int]:
    conn = connect_main()
    try:
        rows = conn.execute("SELECT id FROM projects").fetchall()
    finally:
        conn.close()
    return [r["id"] for r in rows]


def clear_cache() -> int:
    """Wipe every cached tile across all projects. Returns total rows deleted."""
    total = 0
    for pid in _project_ids():
        path = cache_path(pid)
        if not path.exists():
            continue
        conn = _open_cache(pid)
        try:
            cur = conn.execute("DELETE FROM tiles")
            total += cur.rowcount or 0
        finally:
            conn.close()
    return total


def cache_stats() -> dict:
    """Aggregate cache metrics across all projects for the maintenance dashboard.
    `rendered` are rows whose render produced painted pixels; `empty` are rows
    we visited and confirmed are blank (stored as NULL)."""
    rendered = empty = file_size = 0
    base = _resolve_cache_base()
    for pid in _project_ids():
        path = cache_path(pid)
        if not path.exists():
            continue
        conn = _open_cache(pid)
        try:
            rendered += conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE tile_data IS NOT NULL"
            ).fetchone()[0]
            empty += conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE tile_data IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        file_size += path.stat().st_size
    return {
        "path": str(base),
        "file_size_bytes": file_size,
        "rendered_tiles": int(rendered),
        "empty_tiles": int(empty),
        "min_zoom": min_zoom(),
        "max_zoom": max_zoom(),
    }
