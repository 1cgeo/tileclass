"""Pause/resume endpoint coverage and side-effect verification."""
import numpy as np
import pytest
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1, missing=False):
    """Return 65536-byte mask. If missing=True, leave half the pixels as 255."""
    arr = np.full(65536, fill, dtype=np.uint8)
    if missing:
        arr[::2] = 255
    return arr.tobytes()


def _pause(client, tok, tid, body, version=None):
    headers = {**h(tok), "Content-Type": "application/octet-stream"}
    if version is not None:
        headers["X-Tile-Version"] = str(version)
    return client.post(f"/api/tiles/{tid}/pause", headers=headers, content=body)


def _resume(client, tok, tid):
    return client.post(f"/api/tiles/{tid}/resume", headers=h(tok))


def _classify(client, tok, tid, fill=1):
    return client.post(f"/api/tiles/{tid}/classify",
                       headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask(fill))


# ---------- Pause: persistence + state ----------

def test_pause_persists_partial_mask_and_sets_paused_at(client, operators, tiles):
    from backend.database import connect
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    body = _mask(fill=3, missing=True)
    r = _pause(client, t, tile["id"], body, version=tile["version"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["paused_at"] is not None
    assert data["status"] == "in_progress"
    assert data["assigned_to"] == operators[0]["id"]
    # version bumped
    assert data["version"] == tile["version"] + 1

    conn = connect()
    try:
        row = conn.execute(
            "SELECT data_png, paused_at, status, assigned_to FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
        log = conn.execute(
            "SELECT action FROM action_log WHERE tile_id=? ORDER BY id",
            (tile["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert row["paused_at"] is not None
    assert row["status"] == "in_progress"
    assert row["assigned_to"] == operators[0]["id"]
    assert decode_mask(row["data_png"]) == body
    assert [r["action"] for r in log] == ["assign_classify", "pause"]


def test_pause_accepts_partial_mask(client, operators, tiles):
    """Pause must NOT require all-filled (unlike classify/review)."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    # All 255 (totally empty) is still valid for pause
    body = np.full(65536, 255, dtype=np.uint8).tobytes()
    r = _pause(client, t, tile["id"], body, version=tile["version"])
    assert r.status_code == 200


def test_pause_rejects_non_assignee(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    r = _pause(client, t2, tile["id"], _mask(), version=tile["version"])
    assert r.status_code == 403


def test_pause_rejects_wrong_status(client, operators, tiles):
    """Cannot pause a tile that is pending/classified/reviewed/problem."""
    from backend.database import connect, transaction
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    # Force tile to classified (still assigned_to op1 for the auth check)
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET status='classified' WHERE id=?", (tile["id"],),
        )
    r = _pause(client, t, tile["id"], _mask(), version=tile["version"])
    assert r.status_code == 409


def test_pause_rejects_invalid_byte_values(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    bad = bytes([7] * 65536)  # 7 is not in {1..6, 255}
    r = _pause(client, t, tile["id"], bad, version=tile["version"])
    assert r.status_code == 400


def test_pause_rejects_wrong_size(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = _pause(client, t, tile["id"], b"\x01" * 100, version=tile["version"])
    assert r.status_code == 422  # short body fails the global mask-size check


def test_pause_rejects_stale_version(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = _pause(client, t, tile["id"], _mask(), version=tile["version"] + 99)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "tile_modified"


def test_pause_works_for_in_review_too(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    rev = client.get("/api/tiles/next", headers=h(t2)).json()
    assert rev["status"] == "in_review"
    r = _pause(client, t2, rev["id"], _mask(fill=2, missing=True), version=rev["version"])
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"


# ---------- Resume ----------

def test_resume_clears_paused_at_and_logs(client, operators, tiles):
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    p = _pause(client, t, tile["id"], _mask(), version=tile["version"])
    assert p.status_code == 200
    paused_version = p.json()["version"]

    r = _resume(client, t, tile["id"])
    assert r.status_code == 200
    data = r.json()
    assert data["paused_at"] is None
    assert data["status"] == "in_progress"
    assert data["version"] == paused_version + 1

    conn = connect()
    try:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM action_log WHERE tile_id=? ORDER BY id",
            (tile["id"],),
        ).fetchall()]
    finally:
        conn.close()
    assert actions == ["assign_classify", "pause", "resume"]


def test_resume_rejects_non_paused_tile(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    # No pause issued — resume must fail
    r = _resume(client, t, tile["id"])
    assert r.status_code == 409


def test_resume_rejects_non_assignee(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _pause(client, t1, tile["id"], _mask(), version=tile["version"])
    r = _resume(client, t2, tile["id"])
    assert r.status_code == 403


# ---------- Resume is explicit, not implicit ----------

def test_get_assigned_does_not_clear_paused_at(client, operators, tiles):
    """Reading /api/tiles/assigned must not trigger an implicit resume.
    The frontend uses paused_at to decide whether to show the resume prompt."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    # Multiple calls to /assigned must keep paused_at set and not log resume
    for _ in range(3):
        r = client.get("/api/tiles/assigned", headers=h(t))
        assert r.status_code == 200
        assert r.json()["paused_at"] is not None

    conn = connect()
    try:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM action_log WHERE tile_id=? ORDER BY id",
            (tile["id"],),
        ).fetchall()]
    finally:
        conn.close()
    assert "resume" not in actions


def test_get_next_returns_paused_tile_without_resuming(client, operators, tiles):
    """/api/tiles/next must also return the paused tile but not auto-resume.
    Otherwise hitting 'Próximo' would silently reset the timer without the
    user explicitly opting back in."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    nxt = client.get("/api/tiles/next", headers=h(t)).json()
    assert nxt["id"] == tile["id"]
    assert nxt["paused_at"] is not None

    conn = connect()
    try:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM action_log WHERE tile_id=? ORDER BY id",
            (tile["id"],),
        ).fetchall()]
    finally:
        conn.close()
    assert "resume" not in actions


# ---------- Cleanup of paused_at by other transitions ----------

def test_problem_during_pause_clears_paused_at(client, operators, tiles):
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": "bad"})
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at FROM tiles WHERE id=?", (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "problem"
    assert row["paused_at"] is None


def test_admin_unassign_clears_paused_at(client, admin_user, operators, tiles):
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/unassign", headers=h(adm))
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at, assigned_to FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["paused_at"] is None
    assert row["assigned_to"] is None


def test_admin_reset_clears_paused_at(client, admin_user, operators, tiles):
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at FROM tiles WHERE id=?", (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["paused_at"] is None


def test_classify_after_resume_clears_paused_at(client, operators, tiles):
    """Defensive: even if a bug left paused_at set, classify must clear it."""
    from backend.database import connect, transaction
    from backend.mask_utils import empty_mask_png

    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    # Simulate paused_at lingering after resume (shouldn't happen but be defensive)
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET paused_at='2024-01-01T00:00:00+00:00' WHERE id=?",
            (tile["id"],),
        )

    _classify(client, t, tile["id"])
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at FROM tiles WHERE id=?", (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["paused_at"] is None


# ---------- Admin filter ----------

def test_admin_tiles_filter_paused(client, admin_user, operators, tiles):
    """Admin can filter the tile list to show only paused tiles."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _pause(client, t, tile["id"], _mask(), version=tile["version"])

    adm = token(client, "admin", "admin123")
    r = client.get("/api/admin/tiles?paused=true", headers=h(adm))
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["id"] == tile["id"]
    assert items[0]["paused_at"] is not None

    r = client.get("/api/admin/tiles?paused=false", headers=h(adm))
    items = r.json()
    assert len(items) == 9
    assert all(it["id"] != tile["id"] for it in items)
