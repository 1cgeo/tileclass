"""Edge cases across the 5 newly-added features.

Goal: pin behavior at the boundaries — concurrency, idempotency, value
limits, interaction with admin operations, and the side-effects readers
shouldn't have to reverse-engineer from the diff.
"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi.testclient import TestClient
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


# ---- Helpers shared across features ----------------------------------------

def _classify_for(client, op, class_id: int = 1) -> int:
    op_tok = token(client, op["username"], op["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    raw = bytes([class_id]) * 65536
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert r.status_code == 200, r.text
    return nxt["id"]


def _pickup_review(client, op, tile_id: int) -> str:
    """`op` pulls the review queue and asserts they got the expected tile.
    Returns the user's token for follow-up actions."""
    op_tok = token(client, op["username"], op["password"])
    review = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    assert review["id"] == tile_id and review["status"] == "in_review"
    return op_tok


# ---- 1. request_changes edge cases -----------------------------------------

def test_request_changes_preserves_mask_bytes(client, admin_user, operators, tiles):
    """Mask must be byte-identical after kick-back; only status changes."""
    tile_id = _classify_for(client, operators[0], class_id=2)
    from backend.database import connect
    conn = connect()
    try:
        before = conn.execute(
            "SELECT data_png FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()["data_png"]
    finally:
        conn.close()

    op2_tok = _pickup_review(client, operators[1], tile_id)
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "x"},
        headers=h(op2_tok),
    )
    conn = connect()
    try:
        after = conn.execute(
            "SELECT data_png FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()["data_png"]
    finally:
        conn.close()
    assert before == after  # bytes preserved


def test_latest_review_note_after_multiple_cycles(client, admin_user, operators, tiles):
    """Two request_changes cycles → /review-note serves the SECOND note."""
    tile_id = _classify_for(client, operators[0])
    op2_tok = _pickup_review(client, operators[1], tile_id)
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "primeira nota"},
        headers=h(op2_tok),
    )
    # op1 picks it up again, reclassifies, sends to review.
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op1_tok)).json()
    assert nxt["id"] == tile_id  # FIFO — same tile back
    raw = bytes([3]) * 65536
    client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op1_tok), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    op2_tok = _pickup_review(client, operators[1], tile_id)
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "segunda nota"},
        headers=h(op2_tok),
    )
    note = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok)).json()
    assert note["note"] == "segunda nota"


def test_request_changes_preserves_classified_by(client, admin_user, operators, tiles):
    """The original classifier stays linked so dashboard durations remain
    paired against them through the rework cycle."""
    tile_id = _classify_for(client, operators[0])
    op2_tok = _pickup_review(client, operators[1], tile_id)
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "ajustar"},
        headers=h(op2_tok),
    )
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT classified_by, assigned_to FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["classified_by"] == operators[0]["id"]
    assert row["assigned_to"] is None  # anyone can pick it up


def test_request_changes_rejects_over_2000_chars(client, admin_user, operators, tiles):
    """Notes are capped at 2000 chars by the request schema (same as
    /report-problem). The operator gets a clear 422 instead of silently
    losing characters past the limit."""
    tile_id = _classify_for(client, operators[0])
    op2_tok = _pickup_review(client, operators[1], tile_id)
    r = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "x" * 2001},
        headers=h(op2_tok),
    )
    assert r.status_code == 422
    # The 2000-char boundary is accepted — must classify first to pick it
    # back up since the rejected attempt didn't change state.
    boundary = "y" * 2000
    r = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": boundary},
        headers=h(op2_tok),
    )
    assert r.status_code == 200
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    note = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok)).json()
    assert note["note"] == boundary


def test_request_changes_cannot_be_called_by_non_assignee(client, admin_user, operators, tiles):
    """Only the reviewer holding the tile can kick it back. A second
    operator with reviewer rights but not the assignee gets 403."""
    tile_id = _classify_for(client, operators[0])
    _pickup_review(client, operators[1], tile_id)  # op2 holds the tile
    # op3 is also a reviewer (per fixture), tries to kick back a tile they
    # don't own.
    op3_tok = token(client, operators[2]["username"], operators[2]["password"])
    r = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "n"},
        headers=h(op3_tok),
    )
    assert r.status_code == 403


def test_request_changes_logs_action_with_note(client, admin_user, operators, tiles):
    """action_log gets a `request_changes` entry with the note in detail —
    the audit trail is the only persistent record of historical notes."""
    tile_id = _classify_for(client, operators[0])
    op2_tok = _pickup_review(client, operators[1], tile_id)
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "verificar borda NE"},
        headers=h(op2_tok),
    )
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            """SELECT user_id, detail FROM action_log
               WHERE tile_id=? AND action='request_changes' ORDER BY id DESC LIMIT 1""",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["user_id"] == operators[1]["id"]
    assert row["detail"] == "verificar borda NE"


# ---- 2. class_distribution edge cases --------------------------------------

def test_class_counts_excludes_unfilled_pixels(client, admin_user, operators, tmp_path):
    """In a loose project (mask_complete_required=False), 255 pixels are
    saved as part of the mask but MUST NOT show up in class_counts."""
    adm = token(client, admin_user["username"], admin_user["password"])
    stub = tmp_path / "stub.mbtiles"; stub.write_bytes(b"\x00")
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "loose",
            "primary_mbtiles": str(stub),
            "mask_complete_required": False,
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(adm),
    ).json()
    pid = new["id"]
    op = operators[0]
    client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(adm),
    )
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status, data_png)
               VALUES (?, 'loose-tile', 0, 0, 0.1, 0.1, 'pending', ?)""",
            (pid, empty_mask_png()),
        )
    finally:
        conn.close()
    op_tok = token(client, op["username"], op["password"])
    nxt = client.get(f"/api/tiles/next?project_id={pid}", headers=h(op_tok)).json()
    arr = np.full(65536, 255, dtype=np.uint8)
    arr[:1000] = 1
    client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    conn = connect()
    try:
        cc = json.loads(conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["class_counts"])
    finally:
        conn.close()
    # Only class 1 appears; the 64536 unfilled pixels do not.
    assert cc == {"1": 1000}


def test_class_counts_recomputed_on_review_resubmit(client, admin_user, operators, tiles):
    """When the reviewer submits a different mask, class_counts is rewritten."""
    tile_id = _classify_for(client, operators[0], class_id=1)
    op2_tok = _pickup_review(client, operators[1], tile_id)
    new_raw = bytes([3]) * 65536
    client.post(
        f"/api/tiles/{tile_id}/review",
        headers={**h(op2_tok), "Content-Type": "application/octet-stream"},
        content=new_raw,
    )
    from backend.database import connect
    conn = connect()
    try:
        cc = json.loads(conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()["class_counts"])
    finally:
        conn.close()
    assert cc == {"3": 65536}


def test_admin_reset_clears_class_counts(client, admin_user, operators, tiles):
    """Resetting a tile must wipe its class_counts so the dashboard's
    distribution panel doesn't keep counting pixels of a mask that is
    no longer there."""
    tile_id = _classify_for(client, operators[0], class_id=2)
    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        f"/api/admin/tiles/{tile_id}/reset",
        json={"reason": "redo"},
        headers=h(adm),
    )
    assert r.status_code == 200
    from backend.database import connect
    conn = connect()
    try:
        cc = conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()["class_counts"]
    finally:
        conn.close()
    assert cc is None


def test_class_distribution_endpoint_returns_empty_when_no_submissions(client, admin_user):
    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/admin/class-distribution", headers=h(adm))
    assert r.status_code == 200
    assert r.json() == []


def test_recompute_class_counts_idempotent(client, admin_user, operators, tiles):
    """Running the backfill twice produces identical counts (no double-add).
    Submitting writes the cache, recompute matches it, --force overwrites
    with the same value."""
    tile_id = _classify_for(client, operators[0], class_id=4)
    from backend.database import connect, transaction
    from backend.scripts.recompute_class_counts import main as recompute
    import sys

    def _read():
        conn = connect()
        try:
            return conn.execute(
                "SELECT class_counts FROM tiles WHERE id=?", (tile_id,)
            ).fetchone()["class_counts"]
        finally:
            conn.close()

    fresh = _read()  # already populated by submit
    # Wipe + recompute → should match.
    with transaction("IMMEDIATE") as conn:
        conn.execute("UPDATE tiles SET class_counts=NULL WHERE id=?", (tile_id,))
    old_argv = sys.argv
    sys.argv = ["recompute"]
    try:
        recompute()
    finally:
        sys.argv = old_argv
    after = _read()
    assert json.loads(after) == json.loads(fresh)
    # --force on top of populated cache: still matches.
    sys.argv = ["recompute", "--force"]
    try:
        recompute()
    finally:
        sys.argv = old_argv
    assert _read() == after


# ---- 3. heartbeat / auto-pause edge cases ----------------------------------

def test_heartbeat_does_not_bump_version(client, admin_user, operators, tiles):
    """Heartbeat is a side-channel — touching version would cause the next
    submit to fail with tile_modified. Must update only last_heartbeat_at."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    v_before = nxt["version"]
    for _ in range(3):
        client.post(f"/api/tiles/{nxt['id']}/heartbeat", headers=h(op_tok))
    from backend.database import connect
    conn = connect()
    try:
        v_after = conn.execute(
            "SELECT version FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["version"]
    finally:
        conn.close()
    assert v_after == v_before


def test_submit_after_auto_pause_succeeds_and_closes_the_pause(client, admin_user, operators, tiles):
    """The tile stays assigned to the operator while auto-paused, so a submit
    with the original version token succeeds. It logs an implicit `resume`
    so the dashboard subtracts the away time from the cycle duration."""
    op_a = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    from backend.database import transaction, connect
    past = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?", (past, nxt["id"])
        )
    op_b = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op_b))  # triggers sweep

    raw = bytes([1]) * 65536
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={
            **h(op_a), "Content-Type": "application/octet-stream",
            "X-Tile-Version": str(nxt["version"]),
        },
        content=raw,
    )
    assert r.status_code == 200, r.text
    conn = connect()
    try:
        row = conn.execute("SELECT status, paused_at FROM tiles WHERE id=?", (nxt["id"],)).fetchone()
        actions = [(a["action"], a["detail"]) for a in conn.execute(
            "SELECT action, detail FROM action_log WHERE tile_id=? ORDER BY id", (nxt["id"],))]
    finally:
        conn.close()
    assert row["status"] == "classified" and row["paused_at"] is None
    assert actions == [("assign_classify", None), ("pause", "auto"),
                       ("resume", None), ("classify", None)]


def test_auto_paused_tile_auto_resumes_via_next(client, admin_user, operators, tiles):
    """Operator returns and hits /next — their auto-paused tile is the
    only thing assigned to them, and `/next` auto-resumes it (logs a
    `resume` action and clears paused_at)."""
    op_a = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    from backend.database import transaction, connect
    past = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?", (past, nxt["id"])
        )
    # Trigger sweep (independent operator).
    op_b = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op_b))

    # op_a comes back — /next should hand them the same tile, unpaused.
    resumed = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    assert resumed["id"] == nxt["id"]
    assert resumed["paused_at"] is None
    # action_log gets the resume entry so dashboard subtracts the auto-pause
    # interval out of cycle duration.
    conn = connect()
    try:
        last_action = conn.execute(
            """SELECT action FROM action_log
               WHERE tile_id=? AND user_id=? ORDER BY id DESC LIMIT 1""",
            (nxt["id"], operators[0]["id"]),
        ).fetchone()
    finally:
        conn.close()
    assert last_action["action"] == "resume"


def test_manual_pause_not_resumed_by_next(client, admin_user, operators, tiles):
    """Operator's own pause must survive /next (otherwise hitting Próximo
    would silently restart the timer they intentionally paused)."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    arr = np.full(65536, 255, dtype=np.uint8); arr[:50] = 1
    client.post(
        f"/api/tiles/{nxt['id']}/pause",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    # /next returns the paused tile but does NOT clear paused_at.
    again = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    assert again["id"] == nxt["id"]
    assert again["paused_at"] is not None


def test_concurrent_next_calls_pause_each_stale_tile_once(client, admin_user, operators_10, tiles_many):
    """Sweep must be idempotent under concurrent /next pulls — N operators
    racing must not produce N pause log entries for the same stale tile.
    Mirrors the threading pattern of test_concurrency.py: acquire tokens
    sequentially (with rate-limit reset) then race only the /next calls."""
    from backend.main import app
    from backend.auth import reset_rate_limits
    from backend.database import transaction, connect
    from concurrent.futures import ThreadPoolExecutor

    # Tokens for 5 operators, sequential to avoid the auth rate-limit.
    tokens = []
    for op in operators_10[:6]:
        reset_rate_limits()
        tokens.append(token(client, op["username"], op["password"]))

    # First operator picks up a tile; backdate its heartbeat so the next
    # /next call (from any other operator) triggers the auto-pause sweep.
    nxt = client.get(
        "/api/tiles/next?project_id=1", headers=h(tokens[0])
    ).json()
    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?", (past, nxt["id"])
        )

    def hit(tok):
        with TestClient(app) as c:
            return c.get(
                "/api/tiles/next?project_id=1",
                headers={"Authorization": f"Bearer {tok}"},
            ).status_code

    # 5 concurrent /next calls — each opens BEGIN IMMEDIATE in turn, so the
    # sweep runs at most once per call and the tile flips exactly once.
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(hit, tokens[1:]))

    conn = connect()
    try:
        pause_count = conn.execute(
            "SELECT COUNT(*) c FROM action_log "
            "WHERE tile_id=? AND action='pause' AND detail='auto'",
            (nxt["id"],),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert pause_count == 1


# ---- 4. clone edge cases ---------------------------------------------------

def test_clone_is_independent_after_creation(client, admin_user):
    """Editing the clone's classes must not propagate back to the source."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post("/api/admin/projects/1/clone",
                       json={"name": "indep"}, headers=h(tok)).json()
    new_id = new["id"]
    # Patch a class in the clone.
    client.put(
        f"/api/admin/projects/{new_id}/classes",
        json={"classes": [{"id": 1, "name": "AGUA-MOD", "color": "#001122"}]},
        headers=h(tok),
    )
    # Source is untouched.
    src = client.get("/api/projects/1", headers=h(tok)).json()
    assert next(c["name"] for c in src["classes"] if c["id"] == 1) != "AGUA-MOD"


def test_clone_of_remote_url_layer_preserves_url(client, admin_user):
    """Cloning a project with a Martin/TileServer URL keeps it verbatim."""
    tok = token(client, admin_user["username"], admin_user["password"])
    src = client.post(
        "/api/admin/projects",
        json={
            "name": "remote-src",
            "primary_mbtiles": "https://martin.example.com/sat/{z}/{x}/{y}.webp",
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(tok),
    ).json()
    cloned = client.post(
        f"/api/admin/projects/{src['id']}/clone",
        json={"name": "remote-clone"}, headers=h(tok),
    ).json()
    assert cloned["primary_mbtiles"] == src["primary_mbtiles"]


def test_clone_of_inactive_project_is_active(client, admin_user):
    """Cloning an archived project gives a fresh active=True one — admins
    use clone+rename to revive a configuration without reopening the
    archive."""
    tok = token(client, admin_user["username"], admin_user["password"])
    client.patch("/api/admin/projects/1", json={"active": False}, headers=h(tok))
    new = client.post("/api/admin/projects/1/clone",
                       json={"name": "revived"}, headers=h(tok)).json()
    assert new["active"] is True


def test_clone_of_missing_source_is_404(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post("/api/admin/projects/9999/clone",
                    json={"name": "x"}, headers=h(tok))
    assert r.status_code == 404


# ---- 5. backup/verify edge cases -------------------------------------------

def test_backup_then_restore_preserves_tiles(app_env, tmp_path, client, admin_user, operators, tiles):
    """Round-trip: classify some tiles, back up the DB, restore into a
    fresh path → row counts and a sampled tile body match."""
    import sqlite3
    _classify_for(client, operators[0], class_id=2)
    out = tmp_path / "round-trip.db"
    from backend.scripts.backup_db import main as backup
    import sys
    old_argv = sys.argv
    sys.argv = ["backup_db", str(out)]
    try:
        backup()
    finally:
        sys.argv = old_argv

    # Restore = open the backup directly; counts and a sample row match.
    src = sqlite3.connect(app_env)
    dst = sqlite3.connect(out)
    try:
        for table in ("projects", "users", "tiles", "action_log"):
            assert (
                src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ), table
        a = src.execute("SELECT data_png FROM tiles WHERE data_png IS NOT NULL LIMIT 1").fetchone()
        b = dst.execute("SELECT data_png FROM tiles WHERE data_png IS NOT NULL LIMIT 1").fetchone()
        assert a[0] == b[0]
    finally:
        src.close(); dst.close()


def test_verify_ignores_inactive_project_paths(app_env, tmp_path):
    """Inactive projects with broken paths are noise — verify_db skips
    them so the report stays focused on operational issues."""
    import sqlite3, sys, importlib
    # Mark default project inactive (its mbtiles paths don't exist on
    # this machine, so without the active filter verify would fail).
    conn = sqlite3.connect(app_env)
    try:
        conn.execute("UPDATE projects SET active=0 WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    if "backend.scripts.verify_db" in sys.modules:
        del sys.modules["backend.scripts.verify_db"]
    old_argv = sys.argv
    sys.argv = ["verify_db", "--quick"]
    try:
        try:
            importlib.import_module("backend.scripts.verify_db").main()
            code = 0
        except SystemExit as e:
            code = int(e.code or 0)
    finally:
        sys.argv = old_argv
    assert code == 0


def test_verify_catches_invalid_remote_url(app_env, default_project, tmp_path):
    """An active project whose URL lost its placeholders fails verify."""
    import sqlite3, sys, importlib
    stub = tmp_path / "stub.mbtiles"; stub.write_bytes(b"")
    conn = sqlite3.connect(app_env)
    try:
        # Replace primary with an http:// URL missing the {z}/{x}/{y} markers.
        conn.execute(
            "UPDATE projects SET primary_mbtiles=?,"
            "secondary_mbtiles=NULL, tertiary_mbtiles=NULL,"
            "ref_mask_primary_mbtiles=NULL, ref_mask_secondary_mbtiles=NULL "
            "WHERE id=1",
            ("https://martin.example.com/sat/0/0/0.png",),
        )
        conn.commit()
    finally:
        conn.close()
    if "backend.scripts.verify_db" in sys.modules:
        del sys.modules["backend.scripts.verify_db"]
    old_argv = sys.argv
    sys.argv = ["verify_db", "--quick"]
    try:
        try:
            importlib.import_module("backend.scripts.verify_db").main()
            code = 0
        except SystemExit as e:
            code = int(e.code or 0)
    finally:
        sys.argv = old_argv
    assert code == 2


def test_verify_passes_with_remote_url_when_placeholders_present(app_env, tmp_path):
    """A well-formed remote URL is treated as configured — no file check."""
    import sqlite3, sys, importlib
    conn = sqlite3.connect(app_env)
    try:
        conn.execute(
            "UPDATE projects SET primary_mbtiles=?,"
            "secondary_mbtiles=NULL, tertiary_mbtiles=NULL,"
            "ref_mask_primary_mbtiles=NULL, ref_mask_secondary_mbtiles=NULL "
            "WHERE id=1",
            ("https://martin.example.com/sat/{z}/{x}/{y}.webp",),
        )
        conn.commit()
    finally:
        conn.close()
    if "backend.scripts.verify_db" in sys.modules:
        del sys.modules["backend.scripts.verify_db"]
    old_argv = sys.argv
    sys.argv = ["verify_db", "--quick"]
    try:
        try:
            importlib.import_module("backend.scripts.verify_db").main()
            code = 0
        except SystemExit as e:
            code = int(e.code or 0)
    finally:
        sys.argv = old_argv
    assert code == 0
