"""Per-project tile geometry: tile_px and meters_per_pixel.

Covers:
  - schema migration / defaults preserve legacy 256 × 2.5 = 640 m semantics
  - project CRUD validates allowed tile_px values
  - tile_px / meters_per_pixel are PATCHable while no tile exists
  - PATCHing geometry after the first tile is rejected with tile_geometry_locked
  - GET /api/projects/{id} surfaces tile_geometry_locked for the admin form
  - mask_utils encode/decode/validate round-trip at non-default tile_px
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import token
from backend import database, mask_utils, project_service


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _real_mbtiles(tmp_path: Path, name: str = "real.mbtiles") -> str:
    path = tmp_path / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT,"
        " tile_data BLOB, PRIMARY KEY(zoom_level, tile_column, tile_row));"
    )
    conn.execute("INSERT INTO metadata VALUES('format','png'),"
                 "('minzoom','0'),('maxzoom','3')")
    conn.commit()
    conn.close()
    return str(path)


# ---- Schema ----------------------------------------------------------------

def test_schema_has_tile_geometry_columns(app_env):
    """init_db creates tile_px / meters_per_pixel with the historical default."""
    conn = database.connect()
    try:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    finally:
        conn.close()
    assert "tile_px" in cols
    assert "meters_per_pixel" in cols
    # Default project (seeded from config.yaml) inherits the column DEFAULTs.
    proj = project_service.get_project(1)
    assert proj["tile_px"] == 256
    assert proj["meters_per_pixel"] == 2.5
    # tile_meters is derived for callers that want the ground span directly.
    assert proj["tile_meters"] == 640.0


# ---- POST validation -------------------------------------------------------

def test_create_project_accepts_custom_tile_geometry(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "small_tiles",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": 128,
        "meters_per_pixel": 1.0,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["tile_px"] == 128
    assert proj["meters_per_pixel"] == 1.0
    assert proj["tile_meters"] == 128.0


def test_create_project_accepts_arbitrary_positive_tile_px(client, admin_user, tmp_path):
    """tile_px is a free positive integer: 226 is just as valid as 256."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "odd_size",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": 226,
        "meters_per_pixel": 2.5,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["tile_px"] == 226
    assert proj["tile_meters"] == 226 * 2.5


@pytest.mark.parametrize("bad", [0, -1, 4097])
def test_create_project_rejects_out_of_range_tile_px(client, admin_user, tmp_path, bad):
    """Pydantic rejects out-of-range tile_px before the request reaches the
    service layer; FastAPI surfaces it as 422."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": f"bad_{bad}",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": bad,
        "meters_per_pixel": 2.5,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 422


def test_create_project_rejects_non_positive_mpp(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "bad_mpp",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": 256,
        "meters_per_pixel": 0,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 422  # Pydantic gt=0 constraint


# ---- PATCH lock -------------------------------------------------------------

def test_patch_geometry_allowed_before_first_tile(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "preflight",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": 256,
        "meters_per_pixel": 2.5,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    proj = client.post("/api/admin/projects", json=body, headers=h(tok)).json()
    pid = proj["id"]
    r = client.patch(
        f"/api/admin/projects/{pid}",
        json={"tile_px": 512, "meters_per_pixel": 1.5},
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    after = client.get(f"/api/projects/{pid}", headers=h(tok)).json()
    assert after["tile_px"] == 512
    assert after["meters_per_pixel"] == 1.5
    assert after["tile_geometry_locked"] is False


def test_patch_geometry_rejected_after_first_tile(client, admin_user, tmp_path):
    """Once any tile exists in the project, tile_px / meters_per_pixel are
    locked: changing them would corrupt the stored mask body and bbox."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "locked",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "tile_px": 256,
        "meters_per_pixel": 2.5,
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    proj = client.post("/api/admin/projects", json=body, headers=h(tok)).json()
    pid = proj["id"]
    # Insert one tile to flip the lock.
    conn = database.connect()
    try:
        conn.execute(
            "INSERT INTO tiles(project_id, name, bbox_west, bbox_south, "
            "bbox_east, bbox_north, status) VALUES (?,?,?,?,?,?,'pending')",
            (pid, "t1", 0.0, 0.0, 0.001, 0.001),
        )
    finally:
        conn.close()
    # Bust the project_service cache so has_any_tile recomputes.
    project_service._invalidate(pid)

    r = client.patch(
        f"/api/admin/projects/{pid}",
        json={"tile_px": 128},
        headers=h(tok),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "tile_geometry_locked"

    # Other PATCH fields stay editable even when geometry is locked.
    r = client.patch(
        f"/api/admin/projects/{pid}",
        json={"description": "ok"},
        headers=h(tok),
    )
    assert r.status_code == 200, r.text


def test_get_project_surfaces_geometry_locked_flag(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "gc_surface",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "classes": [{"id": 1, "name": "a", "color": "#ff0000"}],
    }
    proj = client.post("/api/admin/projects", json=body, headers=h(tok)).json()
    pid = proj["id"]
    payload = client.get(f"/api/projects/{pid}", headers=h(tok)).json()
    assert payload["tile_geometry_locked"] is False
    # Insert tile, expect locked=True.
    conn = database.connect()
    try:
        conn.execute(
            "INSERT INTO tiles(project_id, name, bbox_west, bbox_south, "
            "bbox_east, bbox_north, status) VALUES (?,?,?,?,?,?,'pending')",
            (pid, "t2", 0.0, 0.0, 0.001, 0.001),
        )
    finally:
        conn.close()
    payload2 = client.get(f"/api/projects/{pid}", headers=h(tok)).json()
    assert payload2["tile_geometry_locked"] is True


# ---- mask_utils round-trip at non-default tile_px ---------------------------

@pytest.mark.parametrize("tile_px", [64, 128, 226, 256, 512])
def test_mask_roundtrip_at_arbitrary_tile_px(tile_px):
    """encode_mask / decode_mask must agree at any positive tile_px,
    including non-power-of-two sizes like 226."""
    rng = np.random.default_rng(seed=42)
    arr = rng.integers(0, 7, size=tile_px * tile_px, dtype=np.uint8).tobytes()
    png = mask_utils.encode_mask(arr, tile_px)
    decoded = mask_utils.decode_mask(png, tile_px)
    assert decoded == arr


def test_validate_submission_size_check_uses_tile_px():
    raw = b"\x01" * (128 * 128)
    ok, missing = mask_utils.validate_submission(
        raw, allowed_ids=[1], require_complete=True, tile_px=128,
    )
    assert ok and missing == 0
    # Same raw, but caller forgets to pass tile_px=128 → falls back to 256
    # default and rejects with size mismatch.
    with pytest.raises(ValueError):
        mask_utils.validate_submission(raw, allowed_ids=[1])


# ---- bbox_from_center scales with tile_meters -------------------------------

def test_bbox_from_center_scales_with_tile_meters():
    pytest.importorskip("pyproj", reason="geo helpers require pyproj")
    from backend.geo import bbox_from_center, ground_distance_m
    # Lat 0 (equator) so geodesic vs flat is closest; precision is 1 mm anyway.
    w1, _, e1, _ = bbox_from_center(0.0, 0.0, 640.0)
    w2, _, e2, _ = bbox_from_center(0.0, 0.0, 1280.0)
    span1 = ground_distance_m(0.0, w1, 0.0, e1)
    span2 = ground_distance_m(0.0, w2, 0.0, e2)
    assert abs(span1 - 640.0) < 0.01
    assert abs(span2 - 1280.0) < 0.01
