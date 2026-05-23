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


# ---------- manual pause beats queue pause when both coexist ----------

def test_resume_prefers_manual_pause_over_queue_pause(
    client, admin_user, operators, tiles
):
    """When an operator has BOTH a manually-paused tile AND queue-paused
    tiles assigned (admin bulk-assigned after the manual pause, possibly with
    smaller ids), `/assigned`, `/next-preview` and `/next` must all return the
    manual-paused one. The operator clicked Pause expecting to come back to
    THAT tile; the queue is a convenience layer that shouldn't override intent."""
    op1 = token(client, "op1", "secret123")
    adm = token(client, "admin", "admin123")

    # Snapshot pending tiles in id order so we can pick "older" ids for the queue.
    pending = client.get("/api/admin/tiles?status=pending", headers=h(adm)).json()
    pending.sort(key=lambda t: t["id"])
    # Reserve tiles[2] as the one op1 will manually pause (larger id than the
    # queue tiles); admin will bulk-assign tiles[0] and tiles[1] afterwards.
    manual_id = pending[2]["id"]
    queue_ids = [pending[0]["id"], pending[1]["id"]]

    # op1 claims the manual tile: /next walks by id ASC, so we need to burn
    # through the smaller ids first. Easiest: admin moves tiles[0..1] out of
    # pending temporarily by assigning them to another op, then restore by
    # resetting AFTER op1 gets tiles[2]. Simpler path: assign tile[2] directly.
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": [manual_id], "user_id": operators[0]["id"]})
    # That put tile[2] on op1's queue as queue-paused; unpause it via /next
    # (auto-resume fires for detail='queue').
    t = client.get("/api/tiles/next", headers=h(op1)).json()
    assert t["id"] == manual_id
    assert t["paused_at"] is None

    # Now op1 pauses it MANUALLY (detail=null).
    empty = np.full(65536, 255, dtype=np.uint8).tobytes()
    pr = client.post(f"/api/tiles/{manual_id}/pause",
                     headers={**h(op1), "Content-Type": "application/octet-stream",
                              "X-Tile-Version": str(t["version"])},
                     content=empty)
    assert pr.status_code == 200

    # Admin bulk-assigns the two smaller-id tiles as a personal queue.
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": queue_ids, "user_id": operators[0]["id"]})
    assert r.status_code == 200

    # /assigned must surface the manually-paused tile, not the smallest-id
    # queue-paused one.
    ass = client.get("/api/tiles/assigned", headers=h(op1)).json()
    assert ass["id"] == manual_id, (
        f"expected manual-paused #{manual_id}, got #{ass['id']} — "
        "queue tile shouldn't override manual pause"
    )
    assert ass["paused_at"] is not None

    # /next-preview agrees (peek, no mutation).
    peek = client.get("/api/tiles/next-preview", headers=h(op1)).json()
    assert peek["id"] == manual_id

    # /next ALSO returns the manual-paused one, and does NOT auto-resume it
    # (manual pauses never auto-resume, regardless of ordering).
    log_before = _actions(manual_id)
    nxt = client.get("/api/tiles/next", headers=h(op1)).json()
    assert nxt["id"] == manual_id
    assert nxt["paused_at"] is not None
    log_after = _actions(manual_id)
    # Last action must still be the manual pause — /next did not log a new resume.
    assert log_after == log_before
    assert log_after[-1]["action"] == "pause" and log_after[-1]["detail"] is None

    # The queue tiles stayed paused, untouched.
    for qid in queue_ids:
        assert _paused_at(qid) is not None


def test_manual_pause_from_within_queue_brings_user_back_to_same_tile(
    client, admin_user, operators, tiles
):
    """Scenario reported by an operator: admin pre-loaded a queue of tiles,
    operator pulled the first one via /next (auto-resume), painted some,
    then clicked Pause. On the next login, /assigned must return THAT tile,
    not the next queue-paused one.

    This is subtle because the paused tile has TWO pause log rows: the
    original queue-pause (detail='queue') from the bulk-assign, and the
    manual pause (detail=NULL) the operator just made. The tie-breaker
    reads the MOST RECENT pause log, so the manual one wins."""
    adm = token(client, "admin", "admin123")
    op1 = token(client, "op1", "secret123")

    # Admin bulk-assigns 3 tiles to op1 as a personal queue.
    pending = client.get("/api/admin/tiles?status=pending", headers=h(adm)).json()
    pending.sort(key=lambda t: t["id"])
    queue_ids = [pending[0]["id"], pending[1]["id"], pending[2]["id"]]
    client.post("/api/admin/tiles/assign", headers=h(adm),
                json={"tile_ids": queue_ids, "user_id": operators[0]["id"]})

    # op1 pulls the first queue tile (/next auto-resumes the oldest by id).
    t = client.get("/api/tiles/next", headers=h(op1)).json()
    assert t["id"] == queue_ids[0]
    assert t["paused_at"] is None

    # op1 pauses it manually while the two other queue tiles are still paused.
    partial = np.full(65536, 255, dtype=np.uint8)
    partial[:100] = 3  # some paint, rest empty
    pr = client.post(f"/api/tiles/{t['id']}/pause",
                     headers={**h(op1), "Content-Type": "application/octet-stream",
                              "X-Tile-Version": str(t["version"])},
                     content=partial.tobytes())
    assert pr.status_code == 200

    # Sanity: both the manual one and the queue ones are now paused.
    assert _paused_at(queue_ids[0]) is not None
    assert _paused_at(queue_ids[1]) is not None
    assert _paused_at(queue_ids[2]) is not None
    # And the pause log on the manual tile has two rows: queue then manual.
    pause_logs = [a for a in _actions(queue_ids[0]) if a["action"] == "pause"]
    assert [p["detail"] for p in pause_logs] == ["queue", None]

    # op1 logs out and back in — `/assigned` must return the manually-paused
    # tile (the one the operator was actively working on), not the next
    # smaller-id queue tile.
    ass = client.get("/api/tiles/assigned", headers=h(op1)).json()
    assert ass["id"] == queue_ids[0], (
        f"expected the just-paused tile #{queue_ids[0]}, got #{ass['id']} — "
        "operator would lose their in-flight work to a queue tile"
    )
    assert ass["paused_at"] is not None

    # /next also returns it, and does NOT auto-resume (last pause is manual).
    log_before = _actions(queue_ids[0])
    nxt = client.get("/api/tiles/next", headers=h(op1)).json()
    assert nxt["id"] == queue_ids[0]
    assert nxt["paused_at"] is not None
    assert _actions(queue_ids[0]) == log_before  # no new resume

    # Explicit /resume puts it back in play with the partial mask intact.
    r = client.post(f"/api/tiles/{queue_ids[0]}/resume", headers=h(op1))
    assert r.status_code == 200
    assert r.json()["paused_at"] is None


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


def test_bulk_assign_rejects_non_reviewer_member(
    client, admin_user, operators, tiles
):
    """Bulk assign of a classified tile fails when the chosen user is only an
    operator (not reviewer) in that project. Mirrors the per-tile guard."""
    op1 = token(client, "op1", "secret123")
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**h(op1), "Content-Type": "application/octet-stream"},
                content=raw)

    adm = token(client, "admin", "admin123")
    # Demote op2 from reviewer to plain operator in project 1.
    client.post("/api/admin/projects/1/members", headers=h(adm),
                json={"user_id": operators[1]["id"], "role": "operator"})

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
