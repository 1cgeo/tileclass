"""Admin maintenance endpoints: overview state + overlay-cache wipe."""
from datetime import datetime, timezone

from backend import mask_tile_service as mts
from backend.database import connect
from backend.mask_utils import encode_mask, TILE_SIZE
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


# A bbox roughly 640m wide near -20° latitude (matches mask_tile_service tests).
_BBOX = (-50.0, -20.005760, -49.994240, -20.0)


def _insert_classified(name, fill_class):
    raw = bytes([fill_class]) * (TILE_SIZE * TILE_SIZE)
    png = encode_mask(raw)
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        pid = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, classified_at, data_png)
               VALUES(?,?,?,?,?,?,'classified',?,?)""",
            (pid, name, *_BBOX, now, png),
        )
    finally:
        conn.close()


def test_overview_requires_admin(client, admin_user, operators):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    assert client.get("/api/admin/maintenance/overview",
                      headers=h(op_tok)).status_code == 403


def test_overview_shape(client, admin_user):
    """Whatever the real config opens (depends on the dev's data_external/),
    each mbtiles slot reports a consistent shape: closed → just `open: False`,
    open → all metadata keys present."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = client.get("/api/admin/maintenance/overview", headers=h(tok)).json()
    assert set(body.keys()) == {"mbtiles", "overlay_cache"}
    for key in ("primary", "dsg", "mapbiomas"):
        info = body["mbtiles"][key]
        assert "open" in info, key
        if info["open"]:
            assert set(info) == {"open", "format", "min_zoom", "max_zoom", "path"}, key
        else:
            assert info == {"open": False}, key
    cache = body["overlay_cache"]
    assert cache["rendered_tiles"] == 0
    assert cache["empty_tiles"] == 0
    assert cache["min_zoom"] == 8
    assert cache["max_zoom"] == 18
    assert cache["file_size_bytes"] >= 0


def test_overview_counts_after_render(client, admin_user):
    """Render one populated tile (rendered_tiles=1) and one over empty space
    (empty_tiles=1) — proves the stats split rendered vs empty correctly."""
    _insert_classified("t1", fill_class=1)
    z = 14
    x, y, *_ = mts.wm_tiles_for_bbox(z, *_BBOX)
    mts.get_tile(z, x, y)            # populated → rendered row
    mts.get_tile(z, 0, 0)            # ocean / no overlap → empty row

    tok = token(client, admin_user["username"], admin_user["password"])
    cache = client.get("/api/admin/maintenance/overview",
                       headers=h(tok)).json()["overlay_cache"]
    assert cache["rendered_tiles"] == 1
    assert cache["empty_tiles"] == 1
    assert cache["file_size_bytes"] > 0


def test_clear_requires_admin(client, admin_user, operators):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    assert client.post("/api/admin/maintenance/overlay-cache/clear",
                       headers=h(op_tok)).status_code == 403


def test_clear_wipes_cache(client, admin_user):
    _insert_classified("t1", fill_class=2)
    z = 14
    x, y, *_ = mts.wm_tiles_for_bbox(z, *_BBOX)
    mts.get_tile(z, x, y)
    mts.get_tile(z, 0, 0)

    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post("/api/admin/maintenance/overlay-cache/clear", headers=h(tok))
    assert r.status_code == 200
    assert r.json()["deleted"] == 2

    cache = client.get("/api/admin/maintenance/overview",
                       headers=h(tok)).json()["overlay_cache"]
    assert cache["rendered_tiles"] == 0
    assert cache["empty_tiles"] == 0
