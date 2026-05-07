"""Web Mercator overlay rendering, MBTiles cache, and bbox invalidation.

Hits the same code path as the live admin map: /api/admin/mask-tiles/{z}/{x}/{y}.png
queries `mask_tile_service.get_tile`, which renders by reprojecting every
intersecting TileClass mask via rasterio.warp and caches the result in a
SQLite MBTiles file. Mutation paths (classify, reset, block, ...) call
`safe_invalidate_tile`, which deletes overlapping cache rows so subsequent
requests re-render with fresh data.
"""
from datetime import datetime, timezone
import io
import numpy as np
import pytest
from PIL import Image

from backend import mask_tile_service as mts
from backend.database import connect
from backend.mask_utils import encode_mask, empty_mask_png, TILE_SIZE
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


# ---- WM math sanity ----------------------------------------------------------

def test_wm_tile_bounds_3857_world(app_env):
    """z=0/0/0 covers the full Web Mercator world."""
    half = 20037508.342789244
    assert mts.wm_tile_bounds_3857(0, 0, 0) == (-half, -half, half, half)


def test_wm_tile_bounds_4326_world(app_env):
    """z=0/0/0 in degrees: -180..180 lon, ~±85.0511 lat (WM clip)."""
    west, south, east, north = mts.wm_tile_bounds_4326(0, 0, 0)
    assert west == pytest.approx(-180)
    assert east == pytest.approx(180)
    assert south == pytest.approx(-85.05112877980659, abs=1e-6)
    assert north == pytest.approx(85.05112877980659, abs=1e-6)


def test_wm_tiles_for_bbox_small_region(app_env):
    """A small bbox at z=8 covers a small (1-3 tile) range."""
    x_min, y_min, x_max, y_max = mts.wm_tiles_for_bbox(8, -50.0, -20.1, -49.9, -19.9)
    assert 0 <= x_min <= x_max < (1 << 8)
    assert 0 <= y_min <= y_max < (1 << 8)
    assert x_max - x_min <= 2
    assert y_max - y_min <= 2


# ---- Helpers -----------------------------------------------------------------

def _default_pid(conn):
    return conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]


def _insert_classified(name, bbox, fill_class):
    """Insert a tile in 'classified' state filled uniformly with `fill_class`."""
    raw = bytes([fill_class]) * (TILE_SIZE * TILE_SIZE)
    png = encode_mask(raw)
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        pid = _default_pid(conn)
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, classified_at, data_png)
               VALUES(?,?,?,?,?,?,'classified',?,?)""",
            (pid, name, *bbox, now, png),
        )
        row = conn.execute("SELECT id FROM tiles WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    return row["id"]


def _png_array(png_bytes):
    return np.array(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))


# A bbox roughly 640m wide near -20° latitude (matches a real TileClass tile size).
_BBOX = (-50.0, -20.005760, -49.994240, -20.0)


def _wm_tile_for(bbox, z):
    """Pick a WM tile that contains the bbox (or its leftmost intersection)."""
    x_min, y_min, _, _ = mts.wm_tiles_for_bbox(z, *bbox)
    return z, x_min, y_min


# ---- Render ------------------------------------------------------------------

_PID = 1  # default project seeded by init_db()


def test_render_empty_when_no_tiles(app_env):
    """A WM tile far from any classified data renders fully transparent."""
    arr = _png_array(mts.get_tile(_PID, 8, 100, 100))
    assert (arr[..., 3] == 0).all()


def test_render_paints_class_color(app_env):
    """Classified tile filled with class 1 → blue (#377eb8) opaque pixels in
    the rendered overlay tile that intersects it."""
    _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)
    arr = _png_array(mts.get_tile(_PID, z, x, y))
    blue = (
        (arr[..., 0] == 0x37)
        & (arr[..., 1] == 0x7e)
        & (arr[..., 2] == 0xb8)
        & (arr[..., 3] == 255)
    )
    assert blue.any(), "expected at least one fully-opaque blue pixel"


def test_pending_tile_does_not_render(app_env):
    """Status filter excludes 'pending' (no useful mask yet)."""
    raw = bytes([1]) * (TILE_SIZE * TILE_SIZE)
    conn = connect()
    try:
        pid = _default_pid(conn)
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, data_png) VALUES(?,?,?,?,?,?,'pending',?)""",
            (pid, "pending-tile", *_BBOX, encode_mask(raw)),
        )
    finally:
        conn.close()
    z, x, y = _wm_tile_for(_BBOX, 14)
    arr = _png_array(mts.get_tile(_PID, z, x, y))
    assert (arr[..., 3] == 0).all()


# ---- Cache -------------------------------------------------------------------

def test_cache_hit_skips_rerender(app_env, monkeypatch):
    """Second call returns from cache; _render_tile runs only once."""
    _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)
    calls = {"n": 0}
    real = mts._render_tile

    def counting(pid, zz, xx, yy):
        calls["n"] += 1
        return real(pid, zz, xx, yy)

    monkeypatch.setattr(mts, "_render_tile", counting)
    a = mts.get_tile(_PID, z, x, y)
    b = mts.get_tile(_PID, z, x, y)
    assert a == b
    assert calls["n"] == 1


def test_empty_region_cached_as_null(app_env):
    """Empty WM tiles cache as NULL — second call still serves a transparent
    PNG without re-rendering, and the row exists in the cache file."""
    z, x, y = 14, 100, 100
    a = mts.get_tile(_PID, z, x, y)
    arr = _png_array(a)
    assert (arr[..., 3] == 0).all()
    import sqlite3
    cache = sqlite3.connect(str(mts.cache_path(_PID)))
    try:
        row = cache.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, mts._tms_row(z, y)),
        ).fetchone()
    finally:
        cache.close()
    assert row is not None and row[0] is None


# ---- Invalidation ------------------------------------------------------------

def test_invalidate_bbox_drops_overlapping_rows(app_env):
    """Cache pre-populated, then invalidate_bbox over the same area → rows gone."""
    _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)
    mts.get_tile(_PID, z, x, y)  # populate cache

    import sqlite3
    cache_file = str(mts.cache_path(_PID))
    cache = sqlite3.connect(cache_file)
    try:
        before = cache.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    finally:
        cache.close()
    assert before >= 1

    deleted = mts.invalidate_bbox(_PID, *_BBOX)
    assert deleted >= 1

    cache = sqlite3.connect(cache_file)
    try:
        row = cache.execute(
            "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, mts._tms_row(z, y)),
        ).fetchone()
    finally:
        cache.close()
    assert row is None


def test_classify_invalidates_cache(app_env, client, admin_user, operators):
    """Submitting a classification clears the cache → next overlay request
    reflects the new mask."""
    raw_empty = empty_mask_png()
    conn = connect()
    try:
        pid = _default_pid(conn)
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, data_png) VALUES(?,?,?,?,?,?,'pending',?)""",
            (pid, "t1", *_BBOX, raw_empty),
        )
    finally:
        conn.close()

    z, x, y = _wm_tile_for(_BBOX, 14)
    pre = _png_array(mts.get_tile(_PID, z, x, y))
    assert (pre[..., 3] == 0).all()

    op_token = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next", headers=h(op_token)).json()
    raw = bytes([1]) * (TILE_SIZE * TILE_SIZE)
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_token), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert r.status_code == 200, r.text

    post = _png_array(mts.get_tile(_PID, z, x, y))
    blue = (post[..., 0] == 0x37) & (post[..., 3] == 255)
    assert blue.any(), "expected the classified mask to appear after invalidation"


def test_reset_invalidates_cache(app_env, client, admin_user, operators):
    """Admin reset wipes the mask AND drops cached overlay tiles."""
    tid = _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)

    pre = _png_array(mts.get_tile(_PID, z, x, y))
    assert ((pre[..., 0] == 0x37) & (pre[..., 3] == 255)).any()

    admin_token = token(client, admin_user["username"], admin_user["password"])
    r = client.post(f"/api/admin/tiles/{tid}/reset", json={"reason": "test"},
                    headers=h(admin_token))
    assert r.status_code == 200

    post = _png_array(mts.get_tile(_PID, z, x, y))
    assert (post[..., 3] == 0).all(), "mask should disappear after admin reset"


# ---- HTTP endpoint -----------------------------------------------------------

def test_endpoint_requires_admin(client, admin_user, operators):
    """Operators are rejected; admin gets a PNG."""
    op_token = token(client, operators[0]["username"], operators[0]["password"])
    r = client.get(f"/api/admin/mask-tiles/{_PID}/14/8400/9600.png", headers=h(op_token))
    assert r.status_code in (401, 403)

    admin_token = token(client, admin_user["username"], admin_user["password"])
    r = client.get(f"/api/admin/mask-tiles/{_PID}/14/8400/9600.png", headers=h(admin_token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_endpoint_rejects_zoom_out_of_range(client, admin_user):
    t = token(client, admin_user["username"], admin_user["password"])
    r = client.get(f"/api/admin/mask-tiles/{_PID}/3/0/0.png", headers=h(t))
    assert r.status_code == 404


def test_endpoint_rejects_xy_out_of_range(client, admin_user):
    """At z=8 there are only 256 tiles per axis; x=9999 must 404."""
    t = token(client, admin_user["username"], admin_user["password"])
    r = client.get(f"/api/admin/mask-tiles/{_PID}/8/9999/0.png", headers=h(t))
    assert r.status_code == 404
