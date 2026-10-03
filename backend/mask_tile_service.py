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
import sqlite3
import threading
from pathlib import Path

# Point PROJ at rasterio's proj.db before anything imports CRS code (Windows
# ships conflicting proj.db installs) — see backend/proj_env.py.
from .proj_env import configure_proj_data
configure_proj_data()

import numpy as np
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from . import project_service
from .config import get_config
from .database import connect as connect_main
from .mask_utils import decode_mask

# XYZ output is fixed 256 (Web Mercator standard); source masks are
# reprojected from their native tile_px onto this grid.
XYZ_TILE_PX = 256


_WM_HALF = 20037508.342789244

# Statuses whose data_png we render. Excludes pending/in_progress (no useful
# mask), problem (mask wiped), and blocked (out of distribution).
_VISIBLE_STATUSES = ("classified", "in_review", "reviewed")

_DEFAULT_MIN_Z = 8
_DEFAULT_MAX_Z = 18
_DEFAULT_CACHE_REL = "data/mask_overlay_cache.mbtiles"

_cache_base: Path | None = None
_cache_lock = threading.Lock()

# Per-project invalidation generation. get_tile() captures it before reading
# the main DB and only caches its render if no invalidation happened since —
# otherwise a mutation committed (and invalidated) mid-render would leave the
# stale PNG cached forever. In-process is enough: the app runs one process
# (CLI tools that mutate tiles clear the cache file directly). `_gen_lock`
# makes "check generation + INSERT" atomic w.r.t. "bump generation"; every
# invalidator bumps BEFORE deleting rows, so a render that wins the lock
# writes a row the subsequent DELETE then removes.
_gen: dict[int, int] = {}
_gen_lock = threading.Lock()


def _generation(project_id: int) -> int:
    with _gen_lock:
        return _gen.get(project_id, 0)


def _bump_generation(project_id: int) -> None:
    with _gen_lock:
        _gen[project_id] = _gen.get(project_id, 0) + 1


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
    different palettes/tilesets never share a cache row.

    No `kind` suffix is needed: `kind` is immutable after project creation
    (enforced in project_service.update_project), so a project_id maps to a
    single render pipeline (raster or vector) for the lifetime of its cache."""
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
        img = Image.new("RGBA", (XYZ_TILE_PX, XYZ_TILE_PX), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _TRANSPARENT_PNG = buf.getvalue()
    return _TRANSPARENT_PNG


def _render_tile(project_id: int, z: int, x: int, y: int) -> bytes | None:
    """Dispatch by project kind: raster reprojects PNG masks, classification
    draws a class-name label per tile."""
    proj = project_service.get_project(project_id)
    if (proj or {}).get("kind") == "classification":
        return _render_classification_tile(project_id, z, x, y, proj)
    return _render_raster_tile(project_id, z, x, y)


def _render_raster_tile(project_id: int, z: int, x: int, y: int) -> bytes | None:
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
    dst_transform = from_bounds(left, bottom, right, top, XYZ_TILE_PX, XYZ_TILE_PX)
    acc = np.full((XYZ_TILE_PX, XYZ_TILE_PX), 255, dtype=np.uint8)
    has_data = False

    # All tiles in the project share the same tile_px (geometry is locked
    # once any tile exists), so we look it up once here.
    proj = project_service.get_project(project_id) or {}
    src_px = int(proj.get("tile_px", 256))

    for r in rows:
        try:
            raw = decode_mask(r["data_png"], src_px)
        except (ValueError, OSError):
            continue
        src_arr = np.frombuffer(raw, dtype=np.uint8).reshape(src_px, src_px)
        if not np.any(src_arr != 255):
            continue
        src_transform = from_bounds(
            r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"],
            src_px, src_px,
        )
        dst = np.full((XYZ_TILE_PX, XYZ_TILE_PX), 255, dtype=np.uint8)
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


def _make_lonlat_to_pixel(z: int, x: int, y: int):
    """Build a lon/lat → XYZ-tile pixel projector for a given WM tile.
    Returns (project_fn, span_ok). Used by the classification overlay; kept
    separate so the spherical-Mercator math lives in one place."""
    left_3857, bottom_3857, right_3857, top_3857 = wm_tile_bounds_3857(z, x, y)
    span_x = right_3857 - left_3857
    span_y = top_3857 - bottom_3857
    span_ok = span_x > 0 and span_y > 0

    def project(lon, lat):
        rad_lat = math.radians(max(min(lat, 85.05112878), -85.05112878))
        mx = lon * _WM_HALF / 180.0
        my = math.log(math.tan((90 + math.degrees(rad_lat)) * math.pi / 360)) / math.pi * _WM_HALF
        px = (mx - left_3857) / span_x * XYZ_TILE_PX
        py = (top_3857 - my) / span_y * XYZ_TILE_PX
        return px, py
    return project, span_ok


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """`#rrggbb` (or `rrggbb`) → (r, g, b). Inputs are project class colors,
    already validated at write time."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _render_classification_tile(project_id: int, z: int, x: int, y: int,
                                proj: dict) -> bytes | None:
    """Render the project's classification tiles into a 256×256 RGBA PNG.

    Each project tile shows up as a low-alpha class-color rectangle with the
    class name centered as text in the same color. Tiles too small at the
    current zoom (< ~24 px) skip the text label since it would be unreadable."""
    from PIL import ImageDraw, ImageFont

    west, south, east, north = wm_tile_bounds_4326(z, x, y)
    placeholders = ",".join("?" * len(_VISIBLE_STATUSES))
    conn = connect_main()
    try:
        rows = conn.execute(
            f"""SELECT bbox_west, bbox_south, bbox_east, bbox_north, data_class_id
                FROM tiles
                WHERE project_id=?
                  AND status IN ({placeholders})
                  AND data_class_id IS NOT NULL
                  AND bbox_west < ? AND bbox_east > ?
                  AND bbox_south < ? AND bbox_north > ?""",
            (project_id, *_VISIBLE_STATUSES, east, west, north, south),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    lonlat_to_pixel, span_ok = _make_lonlat_to_pixel(z, x, y)
    if not span_ok:
        return None

    img = Image.new("RGBA", (XYZ_TILE_PX, XYZ_TILE_PX), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    classes_by_id = {c["id"]: c for c in (proj.get("classes") or [])}
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    has_any = False
    for r in rows:
        cls = classes_by_id.get(r["data_class_id"])
        if not cls:
            continue
        rgb = _hex_to_rgb(cls["color"])
        x0, y0 = lonlat_to_pixel(r["bbox_west"], r["bbox_north"])
        x1, y1 = lonlat_to_pixel(r["bbox_east"], r["bbox_south"])
        w = x1 - x0
        hpx = y1 - y0
        # Translucent fill so admins still see area coverage by class.
        draw.rectangle([x0, y0, x1, y1], fill=(*rgb, 60), outline=(*rgb, 200))
        has_any = True
        # Skip the label when the tile is tiny — text would just be noise.
        if w >= 24 and hpx >= 24 and font is not None:
            label = cls["name"]
            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except (AttributeError, OSError):
                tw, th = (len(label) * 6, 11)
            tx = x0 + (w - tw) / 2
            ty = y0 + (hpx - th) / 2
            # Halo around the text so it stays legible on busy backgrounds.
            for dx_, dy_ in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw.text((tx + dx_, ty + dy_), label, fill=(255, 255, 255, 200), font=font)
            draw.text((tx, ty), label, fill=(*rgb, 255), font=font)

    if not has_any:
        return None
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---- Public API ----

def get_tile(project_id: int, z: int, x: int, y: int) -> bytes:
    """PNG for (z,x,y) in `project_id`. Renders + caches on miss; returns the
    transparent PNG when the cache says the tile has no data."""
    row_tms = _tms_row(z, y)
    gen = _generation(project_id)
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
        # Skipped when an invalidation raced this render (see `_gen`): the
        # response may be stale, but the cache never is.
        with _gen_lock:
            if _gen.get(project_id, 0) == gen:
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
    _bump_generation(project_id)
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
            _bump_generation(pid)
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
        total += clear_cache_for_project(pid)
    return total


def clear_cache_for_project(project_id: int) -> int:
    """Wipe cached tiles for a single project. Used when something the renderer
    depends on changes mid-life — palette colors, attribute schema, or the
    underlying mbtiles source — none of which the per-tile invalidation paths
    cover. Returns the row count deleted; silent no-op when the cache file
    doesn't exist yet."""
    # Bump even when the file is missing: a first render may be in flight.
    _bump_generation(project_id)
    path = cache_path(project_id)
    if not path.exists():
        return 0
    try:
        conn = _open_cache(project_id)
    except sqlite3.Error:
        return 0
    try:
        cur = conn.execute("DELETE FROM tiles")
        return cur.rowcount or 0
    finally:
        conn.close()


def delete_cache_for_project(project_id: int) -> None:
    """Hard-remove the cache file for a project being deleted. Frees disk that
    `clear_cache_for_project` would leave reserved (DELETE keeps the SQLite
    pages allocated until VACUUM). Best-effort: a stray cache file is never
    a correctness issue."""
    _bump_generation(project_id)
    path = cache_path(project_id)
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix) if suffix else path
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


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
