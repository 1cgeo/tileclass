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
    # init_db no longer seeds a project; create the standard test project
    # (id=1 + 6 classes) on demand so the overlay-render tests have a palette.
    from tests.conftest import _seed_test_project
    return _seed_test_project(conn)


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


def test_cache_is_isolated_per_project(app_env):
    """Each project caches into its own <base>_p<id>.mbtiles, so two projects
    covering the same XYZ tile with DIFFERENT palettes never share rows / leak
    each other's colors."""
    from tests.conftest import _seed_test_project
    conn = connect()
    try:
        _seed_test_project(conn)  # project 1: class 1 = #377eb8 (blue)
        # Project 2: class 1 = magenta, so a shared row would be visibly wrong.
        conn.execute("INSERT INTO projects(id,name,kind,tile_px,meters_per_pixel,"
                     "mask_complete_required,primary_mbtiles,active,created_at) "
                     "VALUES (2,'p2','raster',256,2.5,1,'',1,'2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO project_classes(project_id,class_id,name,color,ordering) "
                     "VALUES (2,1,'alvo','#ff00ff',0)")
        png = encode_mask(bytes([1]) * (TILE_SIZE * TILE_SIZE))
        now = datetime.now(timezone.utc).isoformat()
        for pid in (1, 2):
            conn.execute(
                "INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
                "status,classified_at,data_png) VALUES (?,?,?,?,?,?, 'classified',?,?)",
                (pid, f"iso{pid}", *_BBOX, now, png),
            )
        conn.commit()
    finally:
        conn.close()

    z, x, y = _wm_tile_for(_BBOX, 14)
    a1 = _png_array(mts.get_tile(1, z, x, y))
    a2 = _png_array(mts.get_tile(2, z, x, y))
    op1 = a1[a1[..., 3] > 0]
    op2 = a2[a2[..., 3] > 0]
    assert len(op1) and len(op2)
    # Project 1 → blue-ish (R<G<B); project 2 → magenta (high R and B, low G).
    assert (op1[:, 2] > op1[:, 0]).any()                      # blue present in p1
    assert ((op2[:, 0] > 200) & (op2[:, 1] < 80) & (op2[:, 2] > 200)).any()  # magenta in p2
    # The two cache files are distinct.
    assert mts.cache_path(1) != mts.cache_path(2)


# ---- Per-project clear / delete cache ---------------------------------------

def _cache_row_count(pid):
    import sqlite3
    path = mts.cache_path(pid)
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    finally:
        conn.close()


def test_clear_cache_for_project_isolated(app_env):
    """clear_cache_for_project wipes only the target project; other projects
    keep their cached rows. Mirrors the cross-project isolation that already
    exists at render time."""
    from tests.conftest import _seed_test_project
    conn = connect()
    try:
        _seed_test_project(conn)
        conn.execute(
            "INSERT INTO projects(id,name,kind,tile_px,meters_per_pixel,"
            "mask_complete_required,primary_mbtiles,active,created_at) "
            "VALUES (2,'p2','raster',256,2.5,1,'',1,'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO project_classes(project_id,class_id,name,color,ordering) "
            "VALUES (2,1,'alvo','#ff00ff',0)"
        )
    finally:
        conn.close()
    # Populate both caches (one empty-region row each is enough).
    mts.get_tile(1, 14, 100, 100)
    mts.get_tile(2, 14, 100, 100)
    assert _cache_row_count(1) == 1
    assert _cache_row_count(2) == 1

    deleted = mts.clear_cache_for_project(1)
    assert deleted == 1
    assert _cache_row_count(1) == 0
    assert _cache_row_count(2) == 1, "sibling project's cache must survive"


def test_clear_cache_for_project_noop_when_missing(app_env):
    """A project with no cache file yet returns 0 — silent no-op."""
    assert mts.clear_cache_for_project(999) == 0


def test_delete_cache_for_project_removes_file(app_env):
    """delete_cache_for_project nukes the .mbtiles file (and WAL siblings) so
    a deleted project doesn't leak disk."""
    mts.get_tile(_PID, 14, 100, 100)
    path = mts.cache_path(_PID)
    assert path.exists()
    mts.delete_cache_for_project(_PID)
    assert not path.exists()


# ---- Project mutation → overlay invalidation --------------------------------

def _seed_project_then_populate_cache():
    """Common arrange: seed default project + cache one rendered row + one
    empty row. Returns (pid, z, x_classified, y_classified, x_empty, y_empty)."""
    _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)
    mts.get_tile(_PID, z, x, y)            # populated row
    mts.get_tile(_PID, 14, 100, 100)       # empty row
    return _PID, z, x, y, 100, 100


def test_set_classes_clears_overlay_cache(app_env, admin_user):
    """Recoloring/renaming classes must drop cached PNGs — they bake the LUT
    into the bytes, so without invalidation the admin keeps seeing the old
    colors. (Class IDs can't be removed while tiles exist; we test rename.)"""
    from backend import project_service
    pid, *_ = _seed_project_then_populate_cache()
    assert _cache_row_count(pid) >= 1
    new_classes = [
        {"id": 1, "name": "Água renomeada", "color": "#377eb8"},
        {"id": 2, "name": "Área edificada", "color": "#e41a1c"},
        {"id": 3, "name": "Floresta", "color": "#4daf4a"},
        {"id": 4, "name": "Campo", "color": "#ffff33"},
        {"id": 5, "name": "Cultivo", "color": "#984ea3"},
        {"id": 6, "name": "Terreno exposto", "color": "#ff7f00"},
    ]
    project_service.set_classes(pid, new_classes, updated_by=admin_user["id"])
    assert _cache_row_count(pid) == 0


def test_update_project_layer_change_clears_cache(app_env, admin_user):
    """Swapping primary_mbtiles or geometry flips the raster pipeline output —
    cached PNGs are no longer faithful renders."""
    from backend import project_service
    pid, *_ = _seed_project_then_populate_cache()
    assert _cache_row_count(pid) >= 1
    # Use a remote-URL value to skip mbtiles-on-disk validation; any swap is
    # enough to prove the invalidation fires.
    project_service.update_project(
        pid,
        fields={"primary_mbtiles": "https://example.com/{z}/{x}/{y}.png"},
        updated_by=admin_user["id"],
    )
    assert _cache_row_count(pid) == 0


def test_update_project_non_layer_field_keeps_cache(app_env, admin_user):
    """Renaming the project (or any field that doesn't affect rendering) must
    NOT bust the overlay cache — that would re-render needlessly on every
    cosmetic edit."""
    from backend import project_service
    pid, *_ = _seed_project_then_populate_cache()
    before = _cache_row_count(pid)
    assert before >= 1
    project_service.update_project(
        pid,
        fields={"description": "novo texto"},
        updated_by=admin_user["id"],
    )
    assert _cache_row_count(pid) == before


def test_endpoint_404_for_missing_project(client, admin_user):
    """Endpoint must distinguish a deleted/inexistent project from an empty
    project — otherwise a typo in the URL silently gets a transparent PNG."""
    t = token(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/admin/mask-tiles/999/14/100/100.png", headers=h(t))
    assert r.status_code == 404


# ---- Render/invalidate race --------------------------------------------------

def _is_color(arr, hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return ((arr[..., 0] == r) & (arr[..., 1] == g) & (arr[..., 2] == b)
            & (arr[..., 3] == 255)).any()


def _set_class(tile_id, cls):
    raw = bytes([cls]) * (TILE_SIZE * TILE_SIZE)
    conn = connect()
    try:
        conn.execute("UPDATE tiles SET data_png=? WHERE id=?", (encode_mask(raw), tile_id))
    finally:
        conn.close()


@pytest.mark.parametrize("invalidator", ["tile", "bbox", "bulk", "clear_project"])
def test_invalidation_during_render_is_not_lost(app_env, monkeypatch, invalidator):
    """A mutation committed + invalidated while a render is in flight (after the
    renderer read the old rows, before it caches the PNG) must not leave the
    stale PNG cached forever. Forced deterministically by mutating inside the
    render."""
    tid = _insert_classified("race", _BBOX, fill_class=1)  # blue #377eb8
    z, x, y = _wm_tile_for(_BBOX, 14)
    real = mts._render_tile
    fired = {"n": 0}

    def render_then_mutate(pid, zz, xx, yy):
        png = real(pid, zz, xx, yy)          # rows read: still class 1
        if fired["n"] == 0:
            fired["n"] += 1
            _set_class(tid, 2)               # red #e41a1c, committed
            if invalidator == "tile":
                mts.safe_invalidate_tile(tid)
            elif invalidator == "bbox":
                mts.safe_invalidate_bbox(_PID, *_BBOX)
            elif invalidator == "bulk":
                mts.safe_invalidate_tiles([tid])
            else:
                mts.clear_cache_for_project(_PID)
        return png

    monkeypatch.setattr(mts, "_render_tile", render_then_mutate)
    first = _png_array(mts.get_tile(_PID, z, x, y))
    assert _is_color(first, "#377eb8")  # in-flight response may be stale…
    # …but it must not have been cached: the next request shows the new class.
    second = _png_array(mts.get_tile(_PID, z, x, y))
    assert _is_color(second, "#e41a1c") and not _is_color(second, "#377eb8")


def test_render_without_invalidation_still_caches(app_env, monkeypatch):
    """The generation guard must not disable caching in the common case."""
    _insert_classified("t1", _BBOX, fill_class=1)
    z, x, y = _wm_tile_for(_BBOX, 14)
    mts.get_tile(_PID, z, x, y)
    assert _cache_row_count(_PID) == 1
