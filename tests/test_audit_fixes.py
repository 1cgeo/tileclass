"""Regression tests for audited bugs in tile_service / admin tile mutations /
dashboard / operator read endpoints. Each test asserts the DB side effect,
not only the HTTP status."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from tests.conftest import token, _FIXTURE_OP_HASH


def h(t):
    return {"Authorization": f"Bearer {t}"}


OCTET = {"Content-Type": "application/octet-stream"}


def _row(tile_id, cols="*"):
    from backend.database import connect
    conn = connect()
    try:
        r = conn.execute(f"SELECT {cols} FROM tiles WHERE id=?", (tile_id,)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _exec(sql, params=()):
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(sql, params)


def _backdate_heartbeat(tile_id, seconds=600):
    past = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    _exec("UPDATE tiles SET last_heartbeat_at=? WHERE id=?", (past, tile_id))


def _mask(fill=1, missing=False):
    arr = np.full(65536, fill, dtype=np.uint8)
    if missing:
        arr[::2] = 255
    return arr.tobytes()


def _classify(client, tok, tid, fill=1):
    r = client.post(f"/api/tiles/{tid}/classify", headers={**h(tok), **OCTET},
                    content=_mask(fill))
    assert r.status_code == 200, r.text
    return r


def _pause(client, tok, tid, body, version=None):
    headers = {**h(tok), **OCTET}
    if version is not None:
        headers["X-Tile-Version"] = str(version)
    return client.post(f"/api/tiles/{tid}/pause", headers=headers, content=body)


def _actions(tile_id, action=None):
    from backend.database import connect
    conn = connect()
    try:
        sql = "SELECT user_id, action, detail FROM action_log WHERE tile_id=?"
        params = [tile_id]
        if action:
            sql += " AND action=?"
            params.append(action)
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------- Bug 1: resume refreshes last_heartbeat_at ----------

def test_manual_resume_refreshes_heartbeat_so_sweep_does_not_repause(
    client, operators, tiles
):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    p = _pause(client, t1, tile["id"], _mask(missing=True), version=tile["version"])
    assert p.status_code == 200, p.text
    # Operator was away for 10 min while paused.
    _backdate_heartbeat(tile["id"], 600)
    r = client.post(f"/api/tiles/{tile['id']}/resume", headers=h(t1))
    assert r.status_code == 200
    v_after_resume = _row(tile["id"])["version"]

    # Another user's /next runs the stale-heartbeat sweep.
    t2 = token(client, "op2", "secret123")
    other = client.get("/api/tiles/next", headers=h(t2)).json()
    assert other["id"] != tile["id"]

    row = _row(tile["id"])
    assert row["paused_at"] is None
    assert row["version"] == v_after_resume
    assert [a for a in _actions(tile["id"], "pause") if a["detail"] == "auto"] == []


def test_auto_pause_and_owner_resume_keep_version(client, operators, tiles):
    """Sweep auto-pause and the owner's own resume are timer bookkeeping —
    they don't touch the mask, so they must not bump `version`. Otherwise an
    operator whose laptop slept (auto-pause) and who then reloads loses the
    unsynced local backup (dropped as 'older than the server tile') and a
    still-open editor gets 409 tile_modified on submit."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    v0 = _row(tile["id"])["version"]
    _backdate_heartbeat(tile["id"], 600)
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))  # sweep auto-pauses op1's tile
    assert _row(tile["id"])["paused_at"] is not None
    assert _row(tile["id"])["version"] == v0
    r = client.post(f"/api/tiles/{tile['id']}/resume", headers=h(t1))
    assert r.status_code == 200
    assert r.json()["version"] == v0 and _row(tile["id"])["version"] == v0
    # The editor's original version token is still valid for submit.
    sub = client.post(
        f"/api/tiles/{tile['id']}/classify",
        headers={**h(t1), "Content-Type": "application/octet-stream",
                 "X-Tile-Version": str(v0)},
        content=_mask(),
    )
    assert sub.status_code == 200, sub.text
    assert _row(tile["id"])["status"] == "classified"


def test_auto_resume_in_next_keeps_version(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    v0 = _row(tile["id"])["version"]
    _backdate_heartbeat(tile["id"], 600)
    client.get("/api/tiles/next", headers=h(token(client, "op2", "secret123")))
    again = client.get("/api/tiles/next", headers=h(t1)).json()  # auto-resume
    assert again["id"] == tile["id"] and again["paused_at"] is None
    assert again["version"] == v0


def test_auto_resume_in_next_refreshes_heartbeat(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _backdate_heartbeat(tile["id"], 600)
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))  # sweep auto-pauses op1's tile
    assert _row(tile["id"])["paused_at"] is not None

    again = client.get("/api/tiles/next", headers=h(t1)).json()  # auto-resume
    assert again["id"] == tile["id"] and again["paused_at"] is None
    v = _row(tile["id"])["version"]

    t3 = token(client, "op3", "secret123")
    client.get("/api/tiles/next", headers=h(t3))  # sweep again
    row = _row(tile["id"])
    assert row["paused_at"] is None
    assert row["version"] == v


def test_admin_assign_sets_heartbeat(client, admin_user, operators, tiles):
    adm = token(client, "admin", "admin123")
    _backdate_heartbeat(1, 3600)
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": [1], "user_id": operators[0]["id"]})
    assert r.status_code == 200, r.text
    ts = _row(1)["last_heartbeat_at"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    assert 0 <= age < 30


# ---------- Bug 2: reviewer pause must not degrade the classifier's mask ----------

def _to_review(client):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"], fill=1)
    t2 = token(client, "op2", "secret123")
    rev = client.get("/api/tiles/next", headers=h(t2)).json()
    assert rev["id"] == tile["id"] and rev["status"] == "in_review"
    return t2, rev


def test_reviewer_pause_with_incomplete_mask_rejected(client, operators, tiles):
    t2, rev = _to_review(client)
    before = _row(rev["id"], "data_png, class_counts, version, paused_at")
    r = _pause(client, t2, rev["id"], _mask(fill=2, missing=True), version=rev["version"])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["error"] == "unfilled_pixels"
    assert d["missing"] == 32768
    assert d["message"] == "Na revisão, só é possível pausar com a máscara completa."
    after = _row(rev["id"], "data_png, class_counts, version, paused_at")
    assert after == before


def test_reviewer_pause_complete_mask_recomputes_class_counts(client, operators, tiles):
    t2, rev = _to_review(client)
    assert json.loads(_row(rev["id"])["class_counts"]) == {"1": 65536}
    r = _pause(client, t2, rev["id"], _mask(fill=2), version=rev["version"])
    assert r.status_code == 200, r.text
    assert json.loads(_row(rev["id"])["class_counts"]) == {"2": 65536}


def test_in_progress_pause_stays_permissive_and_updates_counts(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    r = _pause(client, t1, tile["id"], _mask(fill=3, missing=True), version=tile["version"])
    assert r.status_code == 200, r.text
    assert json.loads(_row(tile["id"])["class_counts"]) == {"3": 32768}


# ---------- Bug 3: bulk dedupe, unblock NULL guard, reset semantics ----------

def test_block_duplicate_ids_keeps_original_status(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/block", headers=h(adm), json={"ids": [1, 1]})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 1
    row = _row(1)
    assert row["status"] == "blocked" and row["blocked_from"] == "pending"
    assert len(_actions(1, "block")) == 1

    r = client.post("/api/admin/tiles/bulk/unblock", headers=h(adm), json={"ids": [1, 1]})
    assert r.status_code == 200, r.text
    row = _row(1)
    assert row["status"] == "pending" and row["blocked_from"] is None


def test_unblock_with_null_blocked_from_is_refused(client, admin_user, tiles):
    _exec("UPDATE tiles SET status='blocked', blocked_from=NULL WHERE id=2")
    adm = token(client, "admin", "admin123")
    assert client.post("/api/admin/tiles/3/block", headers=h(adm)).status_code == 200
    r = client.post("/api/admin/tiles/bulk/unblock", headers=h(adm), json={"ids": [3, 2]})
    assert r.status_code == 409
    assert _row(2)["status"] == "blocked"
    # Atomic: the well-formed blocked tile in the same batch is untouched.
    assert _row(3)["status"] == "blocked" and _row(3)["blocked_from"] == "pending"
    assert _actions(2, "unblock") == [] and _actions(3, "unblock") == []
    r = client.post("/api/admin/tiles/2/unblock", headers=h(adm))
    assert r.status_code == 409
    assert _row(2)["status"] == "blocked"


def test_reset_blocked_tile_clears_blocked_from(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    assert client.post("/api/admin/tiles/1/block", headers=h(adm)).status_code == 200
    r = client.post("/api/admin/tiles/1/reset", headers=h(adm))
    assert r.status_code == 200
    row = _row(1)
    assert row["status"] == "pending" and row["blocked_from"] is None
    # Re-blocking/unblocking works normally afterwards.
    assert client.post("/api/admin/tiles/1/block", headers=h(adm)).status_code == 200
    assert _row(1)["blocked_from"] == "pending"


def test_reset_nonexistent_tile_404(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    assert client.post("/api/admin/tiles/9999/reset", headers=h(adm)).status_code == 404
    v1 = _row(1)["version"]
    r = client.post("/api/admin/tiles/bulk/reset", headers=h(adm), json={"ids": [1, 9999]})
    assert r.status_code == 404
    assert _row(1)["version"] == v1  # atomic: tile 1 untouched
    assert _actions(1, "reset") == []
    from backend.database import connect
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM action_log WHERE tile_id=9999"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_bulk_mutations_dedupe_ids(client, admin_user, operators, tiles):
    adm = token(client, "admin", "admin123")
    uid = operators[0]["id"]
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": [3, 3], "user_id": uid})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 1
    assert len(_actions(3, "assign_classify")) == 1
    v = _row(3)["version"]
    r = client.post("/api/admin/tiles/bulk/unassign", headers=h(adm), json={"ids": [3, 3]})
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert _row(3)["version"] == v + 1
    r = client.post("/api/admin/tiles/bulk/reset", headers=h(adm), json={"ids": [3, 3]})
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert len(_actions(3, "reset")) == 1
    r = client.post("/api/admin/tiles/bulk/report-problem", headers=h(adm),
                    json={"ids": [4, 4], "note": "x"})
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert len(_actions(4, "report_problem")) == 1


# ---------- Bug 4: class_distribution only counts submitted tiles ----------

def test_class_distribution_ignores_pending_and_blocked(client, admin_user, operators, tiles):
    from backend.admin.dashboard import class_distribution
    _exec("UPDATE tiles SET status='pending', class_counts=? WHERE id=1",
          (json.dumps({"1": 100}),))
    _exec("UPDATE tiles SET status='blocked', blocked_from='classified', "
          "class_counts=? WHERE id=2", (json.dumps({"2": 200}),))
    _exec("UPDATE tiles SET status='reviewed', class_counts=? WHERE id=3",
          (json.dumps({"3": 300}),))
    dist = {d["class_id"]: d["pixels"] for d in class_distribution(1)}
    assert dist == {3: 300}


def test_class_distribution_excludes_request_changes_tile(client, operators, tiles):
    from backend.admin.dashboard import class_distribution
    t2, rev = _to_review(client)
    r = client.post(f"/api/tiles/{rev['id']}/request-changes", headers=h(t2),
                    json={"note": "refazer"})
    assert r.status_code == 200
    assert _row(rev["id"])["status"] == "pending"
    assert class_distribution(1) == []


# ---------- Bug 5: read endpoints require project membership ----------

def _outsider(client):
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) "
            "VALUES ('outsider', ?, 'operator', 1, ?)",
            (_FIXTURE_OP_HASH, datetime.now(timezone.utc).isoformat()),
        )
    return token(client, "outsider", "secret123")


def test_read_endpoints_forbid_non_members(client, admin_user, operators, tiles):
    tok = _outsider(client)
    for path in ("/api/tiles/1", "/api/tiles/1/image", "/api/tiles/1/history",
                 "/api/tiles/1/review-note", "/api/tiles/1/classification",
                 "/api/tiles/1/satellite-thumbnail"):
        r = client.get(path, headers=h(tok))
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["detail"]["error"] == "not_project_member", path
    # Missing tile stays 404.
    for path in ("/api/tiles/9999", "/api/tiles/9999/history",
                 "/api/tiles/9999/review-note"):
        assert client.get(path, headers=h(tok)).status_code == 404, path


def test_read_endpoints_allow_members_and_admin(client, admin_user, operators, tiles):
    op = token(client, "op1", "secret123")
    adm = token(client, "admin", "admin123")
    for tok in (op, adm):
        assert client.get("/api/tiles/1", headers=h(tok)).status_code == 200
        assert client.get("/api/tiles/1/image", headers=h(tok)).status_code == 200
        assert client.get("/api/tiles/1/history", headers=h(tok)).status_code == 200
        assert client.get("/api/tiles/1/review-note", headers=h(tok)).status_code == 204


# ---------- Bug 6: operator report_problem gates status + bumps version ----------

def test_operator_report_problem_bumps_version(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    v = _row(tile["id"])["version"]
    r = client.post(f"/api/tiles/{tile['id']}/report-problem", headers=h(t1),
                    json={"note": "nuvem"})
    assert r.status_code == 200
    row = _row(tile["id"])
    assert row["status"] == "problem" and row["version"] == v + 1


def test_operator_report_problem_rejects_non_active_status(client, operators, tiles):
    # Tile stuck with assigned_to set but already classified (e.g. stale row).
    uid = operators[0]["id"]
    _exec("UPDATE tiles SET status='classified', assigned_to=? WHERE id=1", (uid,))
    t1 = token(client, "op1", "secret123")
    r = client.post("/api/tiles/1/report-problem", headers=h(t1), json={"note": "x"})
    assert r.status_code == 409
    assert _row(1)["status"] == "classified"
    assert _actions(1, "report_problem") == []


# ---------- Bug 7: assign_many requires membership in the tile's project ----------

def test_assign_rejects_non_member_atomically(client, admin_user, operators, tiles):
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO projects(id,name,kind,tile_px,meters_per_pixel,"
            "mask_complete_required,primary_mbtiles,active,created_at) "
            "VALUES (2,'p2','raster',256,2.5,1,'',1,'2026-01-01T00:00:00+00:00')"
        )
        conn.execute("INSERT INTO project_classes(project_id,class_id,name,color,ordering) "
                     "VALUES (2,1,'c1','#ff0000',0)")
        conn.execute("INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,"
                     "bbox_north,status) VALUES (2,'p2t',0,0,0.1,0.1,'pending')")
        p2_tile = conn.execute("SELECT id FROM tiles WHERE project_id=2").fetchone()["id"]
    adm = token(client, "admin", "admin123")
    uid = operators[0]["id"]  # member of project 1 only
    r = client.post("/api/admin/tiles/assign", headers=h(adm),
                    json={"tile_ids": [1, p2_tile], "user_id": uid})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "not_project_member"
    for tid in (1, p2_tile):
        row = _row(tid)
        assert row["status"] == "pending" and row["assigned_to"] is None
        assert _actions(tid) == []


# ---------- Bug 8: 7-day window compares timestamps, not strings ----------

def test_rate_per_day_window_boundary(client, admin_user, tiles):
    from backend.admin.dashboard import dashboard, projects_stats
    boundary = datetime.now(timezone.utc) - timedelta(days=7)
    inside = (boundary + timedelta(minutes=10)).isoformat()
    outside = (boundary - timedelta(minutes=10)).isoformat()
    _exec("UPDATE tiles SET status='classified', classified_at=? WHERE id=1", (inside,))
    _exec("UPDATE tiles SET status='classified', classified_at=? WHERE id=2", (outside,))
    assert dashboard(1)["rate_per_day"] == round(1 / 7, 2)
    p1 = next(p for p in projects_stats() if p["id"] == 1)
    assert p1["rate_per_day"] == round(1 / 7, 2)
