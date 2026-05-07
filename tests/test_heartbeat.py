"""Heartbeat + auto-pause: zombie tiles get freed lazily when /next runs.

The editor pings POST /api/tiles/{id}/heartbeat every 60s while a tile is
open; tiles whose last_heartbeat_at is older than the timeout are paused
the next time anyone calls /next.
"""
from datetime import datetime, timedelta, timezone
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _expire_heartbeat(tile_id: int, seconds: int = 600) -> None:
    """Backdate last_heartbeat_at by `seconds` so the auto-pause sweep
    treats the tile as stale."""
    from backend.database import transaction
    past = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?",
            (past, tile_id),
        )


def test_heartbeat_bumps_timestamp(client, admin_user, operators, tiles):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    # Backdate first so we can prove the bump moves it forward.
    _expire_heartbeat(nxt["id"], seconds=600)
    r = client.post(f"/api/tiles/{nxt['id']}/heartbeat", headers=h(op_tok))
    assert r.status_code == 200 and r.json()["ok"] is True
    from backend.database import connect
    conn = connect()
    try:
        ts = conn.execute(
            "SELECT last_heartbeat_at FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["last_heartbeat_at"]
    finally:
        conn.close()
    # ISO timestamp; must be within the last few seconds.
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    assert 0 <= age < 5


def test_heartbeat_403_for_other_user(client, admin_user, operators, tiles):
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op1_tok)).json()
    r = client.post(f"/api/tiles/{nxt['id']}/heartbeat", headers=h(op2_tok))
    assert r.status_code == 403


def test_heartbeat_noop_on_paused_tile(client, admin_user, operators, tiles):
    """Paused tiles already aren't candidates for the sweep — heartbeat
    silently no-ops so the editor's interval doesn't interfere."""
    import numpy as np
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    # Partial mask: 100 painted pixels, rest 255 (unfilled — valid for pause).
    arr = np.full(65536, 255, dtype=np.uint8); arr[:100] = 1
    pr = client.post(
        f"/api/tiles/{nxt['id']}/pause",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    assert pr.status_code == 200, pr.text
    r = client.post(f"/api/tiles/{nxt['id']}/heartbeat", headers=h(op_tok))
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "not_active"}


def test_stale_tile_auto_paused_on_next_call(client, admin_user, operators, tiles):
    """Operator A picks a tile and disappears (simulated by backdating
    last_heartbeat_at). Operator B calls /next — sweep paused A's tile,
    B is handed a different one."""
    op_a = token(client, operators[0]["username"], operators[0]["password"])
    nxt_a = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    _expire_heartbeat(nxt_a["id"], seconds=400)  # > 300s timeout

    op_b = token(client, operators[1]["username"], operators[1]["password"])
    nxt_b = client.get("/api/tiles/next?project_id=1", headers=h(op_b)).json()
    assert nxt_b["id"] != nxt_a["id"]

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at, assigned_to FROM tiles WHERE id=?",
            (nxt_a["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["paused_at"] is not None  # auto-paused
    # Tile remains assigned to A; A can /resume to take it back.
    assert row["assigned_to"] == operators[0]["id"]


def test_pause_log_marks_auto(client, admin_user, operators, tiles):
    """The pause log entry has detail='auto' so the dashboard's cycle-
    duration pairing distinguishes auto-pauses from manual ones."""
    op_a = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    _expire_heartbeat(nxt["id"], seconds=400)

    op_b = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op_b))

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            """SELECT detail FROM action_log
               WHERE tile_id=? AND action='pause' ORDER BY id DESC LIMIT 1""",
            (nxt["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["detail"] == "auto"
