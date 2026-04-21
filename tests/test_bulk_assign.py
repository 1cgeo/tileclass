"""Admin bulk-assign: personal queue of pre-paused tiles.

Invariants:
 - Every bulk-assigned tile starts with paused_at set (and logs `pause` detail
   `"queue"` so `/api/tiles/next` can distinguish it from a manual pause).
 - `/api/tiles/next` prefers a non-paused tile; otherwise it unpauses the
   oldest queued one (FIFO by id) and logs `resume`.
 - `/api/tiles/assigned` and `/api/tiles/next-preview` never mutate state —
   they return the paused tile as-is.
 - A manual pause is NEVER auto-resumed by `/next` (detail != "queue").
 - The lot is atomic: if any tile fails validation, nothing is written.
 - Dashboard duration accounting subtracts the queue-wait window correctly.
"""
import time
from datetime import datetime, timezone, timedelta
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _actions(tile_id):
    from backend.database import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT user_id, action, detail FROM action_log "
            "WHERE tile_id=? ORDER BY id",
            (tile_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _paused_at(tile_id):
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT paused_at FROM tiles WHERE id=?", (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["paused_at"]


# ---------- bulk-assign writes paused + queue log ----------

def test_bulk_assign_marks_every_tile_paused_with_queue_log(
    client, admin_user, operators, tiles
):
    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    ids = [pending[0]["id"], pending[1]["id"], pending[2]["id"]]

    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": ids, "user_id": operators[0]["id"],
                          "reason": "morning batch"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected"] == 3
    assert [it["id"] for it in body["items"]] == ids
    assert all(it["status"] == "in_progress" for it in body["items"])

    for tid in ids:
        assert _paused_at(tid) is not None
        acts = _actions(tid)
        # assign_classify (op) → pause queue (op) → admin_assign (admin)
        assert [a["action"] for a in acts] == ["assign_classify", "pause", "admin_assign"]
        assert acts[0]["user_id"] == operators[0]["id"]
        assert acts[0]["detail"] == "morning batch"
        assert acts[1]["user_id"] == operators[0]["id"]
        assert acts[1]["detail"] == "queue"
        assert acts[2]["user_id"] == admin_user["id"]


def test_bulk_assign_reviewers_to_classified(client, admin_user, operators, tiles):
    """Classified tiles are pre-queued as in_review for the reviewer."""
    op1 = token(client, "op1", "secret123")
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    ids = []
    for _ in range(2):
        t = client.get("/api/tiles/next", headers=h(op1)).json()
        client.post(f"/api/tiles/{t['id']}/classify",
                    headers={**h(op1), "Content-Type": "application/octet-stream"},
                    content=raw)
        ids.append(t["id"])

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": ids, "user_id": operators[1]["id"]})
    assert r.status_code == 200, r.text
    assert [it["status"] for it in r.json()["items"]] == ["in_review", "in_review"]
    for tid in ids:
        acts = _actions(tid)
        # classify cycle from op1 comes first; the bulk triplet is appended at the end.
        assert [a["action"] for a in acts[-3:]] == ["assign_review", "pause", "admin_assign"]
        assert acts[-2]["detail"] == "queue"


# ---------- /next prefers non-paused; auto-resumes queue-paused ----------

def test_next_prefers_non_paused_assigned_tile(client, admin_user, operators, tiles):
    op1 = token(client, "op1", "secret123")
    # op1 pulls one tile normally (active, not paused).
    active = client.get("/api/tiles/next", headers=h(op1)).json()

    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    queue_ids = [pending[0]["id"], pending[1]["id"]]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": queue_ids, "user_id": operators[0]["id"]})

    # /next must still return the active tile, not one of the queued ones.
    nxt = client.get("/api/tiles/next", headers=h(op1)).json()
    assert nxt["id"] == active["id"]
    assert nxt["paused_at"] is None
    # Queue tiles remain paused.
    for tid in queue_ids:
        assert _paused_at(tid) is not None


def test_next_auto_resumes_queue_paused_tile_fifo(
    client, admin_user, operators, tiles
):
    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    # Reverse order to prove FIFO is by id, not by log order.
    queue_ids = [pending[2]["id"], pending[0]["id"], pending[1]["id"]]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": queue_ids, "user_id": operators[0]["id"]})

    op1 = token(client, "op1", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(op1)).json()
    assert nxt["id"] == pending[0]["id"]  # smallest id wins
    assert nxt["paused_at"] is None
    acts = _actions(nxt["id"])
    assert acts[-1]["action"] == "resume"
    assert acts[-1]["user_id"] == operators[0]["id"]


def test_finishing_first_pulls_next_queue_tile_automatically(
    client, admin_user, operators, tiles
):
    """The payoff scenario: admin pre-queues 2 tiles, user finishes one,
    /next hands over the second (auto-resumed). No clicks to unpause."""
    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    ids = [pending[0]["id"], pending[1]["id"]]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": ids, "user_id": operators[0]["id"]})

    op1 = token(client, "op1", "secret123")
    first = client.get("/api/tiles/next", headers=h(op1)).json()
    assert first["id"] == ids[0]
    assert first["paused_at"] is None

    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    r = client.post(f"/api/tiles/{first['id']}/classify",
                    headers={**h(op1), "Content-Type": "application/octet-stream"},
                    content=raw)
    assert r.status_code == 200

    second = client.get("/api/tiles/next", headers=h(op1)).json()
    assert second["id"] == ids[1]
    assert second["paused_at"] is None


# ---------- read-only endpoints never mutate ----------

def test_assigned_and_preview_do_not_unpause_queue_tile(
    client, admin_user, operators, tiles
):
    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    tid = pending[0]["id"]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": [tid], "user_id": operators[0]["id"]})

    op1 = token(client, "op1", "secret123")
    for _ in range(3):
        r = client.get("/api/tiles/assigned", headers=h(op1)).json()
        assert r["id"] == tid
        assert r["paused_at"] is not None
        r = client.get("/api/tiles/next-preview", headers=h(op1)).json()
        assert r["id"] == tid
        assert r["paused_at"] is not None

    # No `resume` was logged.
    acts = [a["action"] for a in _actions(tid)]
    assert "resume" not in acts


# ---------- manual pause is NOT auto-resumed by /next ----------

def test_next_does_not_auto_resume_manual_pause(client, operators, tiles):
    """Regression guard for the existing invariant: if the operator paused
    explicitly, /next returns the tile still paused (no detail='queue')."""
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    # Manual pause (no detail)
    raw = np.full(65536, 255, dtype=np.uint8).tobytes()
    pr = client.post(f"/api/tiles/{tile['id']}/pause",
                     headers={**h(op1), "Content-Type": "application/octet-stream",
                              "X-Tile-Version": str(tile["version"])},
                     content=raw)
    assert pr.status_code == 200

    nxt = client.get("/api/tiles/next", headers=h(op1)).json()
    assert nxt["id"] == tile["id"]
    assert nxt["paused_at"] is not None
    assert "resume" not in [a["action"] for a in _actions(tile["id"])]


# ---------- atomicity ----------

def test_bulk_assign_rollback_on_bad_tile(client, admin_user, operators, tiles):
    """If any tile in the batch has an invalid status, NONE are assigned."""
    # Put one tile into in_progress so it fails the bulk validation.
    op1 = token(client, "op1", "secret123")
    locked = client.get("/api/tiles/next", headers=h(op1)).json()

    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    good = [pending[0]["id"], pending[1]["id"]]
    batch = good + [locked["id"]]

    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": batch, "user_id": operators[1]["id"]})
    assert r.status_code == 409

    # Nothing was assigned to operators[1] — the good tiles stayed pending.
    for tid in good:
        assert _paused_at(tid) is None
        from backend.database import connect
        conn = connect()
        try:
            row = conn.execute(
                "SELECT status, assigned_to FROM tiles WHERE id=?", (tid,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "pending"
        assert row["assigned_to"] is None


def test_bulk_assign_rejects_reviewer_without_can_review(
    client, admin_user, operators, tiles
):
    op1 = token(client, "op1", "secret123")
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**h(op1), "Content-Type": "application/octet-stream"},
                content=raw)

    adm = token(client, "admin", "admin123")
    # Remove can_review from op2.
    client.patch(f"/api/admin/users/{operators[1]['id']}/can-review",
                 headers=h(adm), json={"can_review": False})

    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": [tile["id"]], "user_id": operators[1]["id"]})
    assert r.status_code == 409
    # Tile still classified, nobody assigned.
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, assigned_to, paused_at FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["assigned_to"] is None
    assert row["paused_at"] is None


def test_bulk_assign_rejects_classifier_as_reviewer(
    client, admin_user, operators, tiles
):
    op1 = token(client, "op1", "secret123")
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**h(op1), "Content-Type": "application/octet-stream"},
                content=raw)

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": [tile["id"]], "user_id": operators[0]["id"]})
    assert r.status_code == 409


# ---------- dashboard pairing honors the queue-wait ----------

def test_dashboard_subtracts_queue_wait_from_cycle_duration(
    client, admin_user, operators, tiles
):
    """Bulk-assign logs `pause` (detail=queue) → `resume` (when /next picks it
    up) → `classify`. `_cycle_durations` must subtract the pause→resume
    interval so the reported duration reflects active-work time, not the
    time the tile spent in the personal queue."""
    from backend.database import connect

    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=h(adm)).json()
    tid = pending[0]["id"]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": [tid], "user_id": operators[0]["id"]})

    # Backdate assign_classify + queue-pause to simulate a 1h wait in the queue.
    t_assign = datetime.now(timezone.utc) - timedelta(hours=1, minutes=10)
    t_pause = t_assign  # logged immediately after the assign
    conn = connect()
    try:
        conn.execute(
            "UPDATE action_log SET created_at=? "
            "WHERE tile_id=? AND action='assign_classify'",
            (t_assign.isoformat(), tid),
        )
        conn.execute(
            "UPDATE action_log SET created_at=? "
            "WHERE tile_id=? AND action='pause' AND detail='queue'",
            (t_pause.isoformat(), tid),
        )
        conn.commit()
    finally:
        conn.close()

    op1 = token(client, "op1", "secret123")
    # /next unpauses and logs `resume` now.
    fresh = client.get("/api/tiles/next", headers=h(op1)).json()
    assert fresh["id"] == tid

    # Force the resume timestamp to be ~60s before classify so we get a
    # predictable cycle duration we can assert on.
    t_resume = datetime.now(timezone.utc) - timedelta(seconds=60)
    conn = connect()
    try:
        conn.execute(
            "UPDATE action_log SET created_at=? "
            "WHERE tile_id=? AND action='resume'",
            (t_resume.isoformat(), tid),
        )
        conn.commit()
    finally:
        conn.close()

    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    r = client.post(f"/api/tiles/{tid}/classify",
                    headers={**h(op1), "Content-Type": "application/octet-stream"},
                    content=raw)
    assert r.status_code == 200

    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    # Should be ~60s, NOT ~1h10min. Allow a 20s slack for test scheduling.
    assert d["avg_classify_seconds"] < 120, (
        f"expected ~60s, got {d['avg_classify_seconds']}s — queue wait not subtracted"
    )
    assert d["avg_classify_seconds"] > 30
