"""Audit-driven test sweep: regression for a real bug found in the audit
(stale review note after admin reset) plus the coverage gaps the auditor
flagged across all 5 features.

Each test corresponds to a specific finding in the audit; the docstring
links the assertion to the failure mode that would otherwise slip through.
"""
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi.testclient import TestClient
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _classify(client, op_tok, class_id: int = 1) -> int:
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    raw = bytes([class_id]) * 65536
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert r.status_code == 200, r.text
    return nxt["id"]


# ============================================================================
# 1. request_changes
# ============================================================================

def test_review_note_cleared_after_admin_reset(client, admin_user, operators, tiles):
    """REGRESSION: before the fix, latest_review_note returned the
    pre-reset note even though the cycle that produced it was discarded.
    The next classifier saw ghost feedback for work that no longer existed.

    Fix: only return notes whose created_at >= tile.classified_at, and
    classified_at is null after reset → no note is surfaced."""
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    tile_id = _classify(client, op1_tok)

    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "ajustar limite NE"},
        headers=h(op2_tok),
    )
    # Note is live before the reset.
    pre = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok))
    assert pre.status_code == 200

    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        f"/api/admin/tiles/{tile_id}/reset",
        json={"reason": "discarded"},
        headers=h(adm),
    )
    assert r.status_code == 200

    # Note must NOT surface — the cycle that produced it was discarded.
    post = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok))
    assert post.status_code == 204


def test_review_note_survives_request_changes_chain(client, admin_user, operators, tiles):
    """The fix must not over-prune: in a normal classify→review→
    request_changes→reclassify→review→request_changes chain, every kick-back
    note is from the current cycle and the latest one must still surface."""
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    tile_id = _classify(client, op1_tok)
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "primeira"}, headers=h(op2_tok),
    )
    # op1 picks up + reclassifies.
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op1_tok)).json()
    assert nxt["id"] == tile_id
    client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op1_tok), "Content-Type": "application/octet-stream"},
        content=bytes([2]) * 65536,
    )
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "segunda"}, headers=h(op2_tok),
    )
    note = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok)).json()
    assert note["note"] == "segunda"


def test_request_changes_empty_string_vs_whitespace(client, admin_user, operators, tiles):
    """Boundary: Pydantic min_length=1 rejects "" (422 schema), but
    whitespace-only "   " has length 3 and slips through the schema gate
    — the service-level strip+empty-check returns 400 empty_note. Two
    distinct paths, two distinct error codes."""
    tile_id = _classify(client, token(
        client, operators[0]["username"], operators[0]["password"],
    ))
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))

    r_empty = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": ""},
        headers=h(op2_tok),
    )
    assert r_empty.status_code == 422  # Pydantic schema validation

    r_ws = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "   \t  "},
        headers=h(op2_tok),
    )
    assert r_ws.status_code == 400
    assert r_ws.json()["detail"]["error"] == "empty_note"


def test_request_changes_concurrent_with_classify_attempt(client, admin_user, operators, tiles):
    """Race: reviewer kicks back while the original classifier is still
    holding a stale version client-side and tries to submit. The version
    bump from request_changes must surface as 409 tile_modified, NOT as
    a silent overwrite."""
    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op1_tok)).json()
    stale_version = nxt["version"]
    # op1 classifies (advances version).
    client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op1_tok), "Content-Type": "application/octet-stream"},
        content=bytes([1]) * 65536,
    )
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    client.post(
        f"/api/tiles/{nxt['id']}/request-changes",
        json={"note": "x"},
        headers=h(op2_tok),
    )
    # op1 now picks up the (returned-to-pending) tile, but a second editor
    # tab still has the IN_PROGRESS version cached. Submit with that
    # stale version must 409.
    client.get("/api/tiles/next?project_id=1", headers=h(op1_tok))
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={
            **h(op1_tok),
            "Content-Type": "application/octet-stream",
            "X-Tile-Version": str(stale_version),
        },
        content=bytes([2]) * 65536,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "tile_modified"


# ============================================================================
# 2. class_distribution
# ============================================================================

def test_class_counts_mixed_mask(client, admin_user, operators, tiles):
    """Existing test painted a uniform mask — failure modes in numpy.bincount
    on a single-class array would be invisible. Mixed proportions exercise
    the actual histogram path."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    arr = np.empty(65536, dtype=np.uint8)
    arr[:30000] = 1
    arr[30000:65000] = 3
    arr[65000:] = 2  # 536 pixels
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    assert r.status_code == 200
    from backend.database import connect
    conn = connect()
    try:
        cc = json.loads(conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["class_counts"])
    finally:
        conn.close()
    assert cc == {"1": 30000, "2": 536, "3": 35000}


def test_class_distribution_ordering_pixels_desc(client, admin_user, operators, tiles):
    """Endpoint returns entries sorted by descending pixel count — the
    dashboard renders the top class first, which only matters because the
    endpoint promises it. A bug swapping the sort to ascending would let
    the UI render and look "fine" while showing the wrong leader."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    # Three submissions: class 3 dominates (3× tiles), class 1 next (2×),
    # class 2 last (1×).
    for cid in (3, 3, 3, 1, 1, 2):
        _classify(client, op_tok, class_id=cid)

    adm = token(client, admin_user["username"], admin_user["password"])
    body = client.get("/api/admin/class-distribution", headers=h(adm)).json()
    ids_in_order = [c["class_id"] for c in body]
    assert ids_in_order == [3, 1, 2]


def test_class_distribution_two_projects_same_class_id(client, admin_user, operators, tiles, tmp_path):
    """Same `class_id=1` exists in two projects with different colors.
    Aggregator must keep them separate (key on (project_id, class_id))
    so the dashboard renders one bar per project. Without that, the
    palette would clash and totals would be wrongly merged."""
    adm = token(client, admin_user["username"], admin_user["password"])
    stub = tmp_path / "stub.mbtiles"; stub.write_bytes(b"\x00")
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "alt",
            "primary_mbtiles": str(stub),
            "classes": [{"id": 1, "name": "outra-agua", "color": "#11ee22"}],
        },
        headers=h(adm),
    ).json()
    pid2 = new["id"]
    op = operators[0]
    client.post(
        f"/api/admin/projects/{pid2}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(adm),
    )
    # Submit class 1 in project 1.
    op_tok = token(client, op["username"], op["password"])
    _classify(client, op_tok, class_id=1)
    # And class 1 in project 2 (need a tile there first).
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status, data_png)
               VALUES (?, 'alt-tile', 0, 0, 0.1, 0.1, 'pending', ?)""",
            (pid2, empty_mask_png()),
        )
    finally:
        conn.close()
    nxt = client.get(f"/api/tiles/next?project_id={pid2}", headers=h(op_tok)).json()
    client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=bytes([1]) * 65536,
    )
    # Global view: two entries with class_id=1, distinct project_id and color.
    body = client.get("/api/admin/class-distribution", headers=h(adm)).json()
    matches = [c for c in body if c["class_id"] == 1]
    assert len(matches) == 2
    assert {m["project_id"] for m in matches} == {1, pid2}
    assert {m["color"] for m in matches} == {"#377eb8", "#11ee22"}


def test_class_distribution_handles_malformed_json(client, admin_user, operators, tiles):
    """If a row's class_counts somehow ends up with malformed JSON (legacy
    bug, manual UPDATE, etc.), the endpoint must skip it instead of 500.
    The try/except in dashboard.class_distribution covers exactly this."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    tile_id = _classify(client, op_tok, class_id=1)
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET class_counts=? WHERE id=?",
            ("{not json", tile_id),
        )
    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/admin/class-distribution", headers=h(adm))
    assert r.status_code == 200  # endpoint did not blow up
    # The corrupt row contributed nothing.
    assert all(c["class_id"] != 1 or c["pixels"] == 0 for c in r.json()) or r.json() == []


# ============================================================================
# 3. heartbeat / auto-pause
# ============================================================================

def test_already_manually_paused_tile_not_swept(client, admin_user, operators, tiles):
    """A tile pre-paused by its operator (manual) must NOT be touched by
    the auto-pause sweep, even when its heartbeat goes stale. Otherwise
    the operator's intentional pause timer would silently restart and
    the cycle-duration metric would lose the manual gap."""
    op_a = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_a)).json()
    arr = np.full(65536, 255, dtype=np.uint8); arr[:50] = 1
    pr = client.post(
        f"/api/tiles/{nxt['id']}/pause",
        headers={**h(op_a), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    assert pr.status_code == 200

    from backend.database import connect, transaction
    conn = connect()
    try:
        original_paused_at = conn.execute(
            "SELECT paused_at FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["paused_at"]
    finally:
        conn.close()

    # Backdate heartbeat 10 min and trigger sweep via op_b.
    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?",
            (past, nxt["id"]),
        )
    op_b = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op_b))

    # paused_at unchanged + no auto pause log entry.
    conn = connect()
    try:
        after = conn.execute(
            "SELECT paused_at FROM tiles WHERE id=?", (nxt["id"],)
        ).fetchone()["paused_at"]
        auto_count = conn.execute(
            "SELECT COUNT(*) c FROM action_log "
            "WHERE tile_id=? AND action='pause' AND detail='auto'",
            (nxt["id"],),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert after == original_paused_at
    assert auto_count == 0


# ============================================================================
# 4. clone_project
# ============================================================================

def test_clone_with_tiles_source_keeps_clone_empty(client, admin_user, operators, tiles):
    """Cloning a project that already has tiles must produce a tile-empty
    clone — the source's queue/work-history is its own. Otherwise hitting
    /next on the clone would hand out tiles the operator never intended
    to classify in the new theme."""
    adm = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects/1/clone",
        json={"name": "tiles-test"},
        headers=h(adm),
    ).json()
    from backend.database import connect
    conn = connect()
    try:
        src_count = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE project_id=1"
        ).fetchone()["c"]
        clone_count = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE project_id=?", (new["id"],)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert src_count == 10  # source preserved
    assert clone_count == 0


def test_clone_double_default_name_collides(client, admin_user):
    """Two clones of the same project without an explicit name BOTH try
    to create `<name>_copia` — the second must 409 instead of silently
    creating `<name>_copia (2)` or overwriting the first."""
    adm = token(client, admin_user["username"], admin_user["password"])
    r1 = client.post("/api/admin/projects/1/clone", json={}, headers=h(adm))
    assert r1.status_code == 200
    r2 = client.post("/api/admin/projects/1/clone", json={}, headers=h(adm))
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "name_taken"


def test_clone_logs_action(client, admin_user):
    """The audit trail entry pairs source→target so a security review can
    reconstruct who copied which configuration when."""
    adm = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects/1/clone",
        json={"name": "logged"},
        headers=h(adm),
    ).json()
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            """SELECT user_id, detail FROM action_log
               WHERE action='project_clone' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()
    assert row["user_id"] == admin_user["id"]
    assert row["detail"] == f"1->{new['id']}"


def test_clone_preserves_class_ordering(client, admin_user):
    """The source's class display order must survive the clone; the
    frontend renders classes in this order, so a re-sort would shuffle
    the operator's muscle-memory shortcut numbers (1=água, 2=edif…)."""
    adm = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects/1/clone",
        json={"name": "ordered"},
        headers=h(adm),
    ).json()
    src = client.get("/api/projects/1", headers=h(adm)).json()
    src_ids = [c["id"] for c in src["classes"]]
    new_ids = [c["id"] for c in new["classes"]]
    assert src_ids == new_ids


# ============================================================================
# 5. backup_db / verify_db
# ============================================================================

def _run_cli(module_path: str, argv: list) -> int:
    if module_path in sys.modules:
        del sys.modules[module_path]
    old_argv = sys.argv
    sys.argv = [module_path.split(".")[-1], *argv]
    try:
        mod = importlib.import_module(module_path)
        try:
            mod.main()
        except SystemExit as e:
            return int(e.code or 0)
    finally:
        sys.argv = old_argv
    return 0


def test_verify_db_catches_corrupt_png(app_env, tmp_path, tiles):
    """A row with non-PNG bytes in data_png must surface as a mask
    decoding error, not as a silent skip — operators rely on verify
    catching corruption that thumbnails would also fail to render."""
    stub = tmp_path / "stub.mbtiles"; stub.write_bytes(b"\x00")
    conn = sqlite3.connect(app_env)
    try:
        conn.execute(
            """UPDATE projects SET primary_mbtiles=?,
               secondary_mbtiles=NULL, tertiary_mbtiles=NULL,
               ref_mask_primary_mbtiles=NULL, ref_mask_secondary_mbtiles=NULL""",
            (str(stub),),
        )
        # Mark a tile as classified with garbage as its data_png.
        conn.execute(
            "UPDATE tiles SET status='classified', data_png=? WHERE id=("
            "SELECT id FROM tiles ORDER BY id LIMIT 1)",
            (b"this-is-not-a-png", ),
        )
        conn.commit()
    finally:
        conn.close()
    code = _run_cli("backend.scripts.verify_db", [])
    assert code == 2


def test_backup_db_missing_source_exits_1(app_env, tmp_path, monkeypatch):
    """Backup of a non-existent DB must exit with the user-error code (1),
    not the integrity-failure code (2). cron jobs use exit codes to
    distinguish between "fix the path" and "data is corrupt"."""
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "_DB_PATH", tmp_path / "does-not-exist.db",
                        raising=True)
    out = tmp_path / "out.db"
    code = _run_cli("backend.scripts.backup_db", [str(out)])
    assert code == 1


def test_verify_quick_still_catches_fk_violation(app_env, tmp_path):
    """--quick skips mask decoding but PRAGMA foreign_key_check still
    runs. A dangling FK must surface even in quick mode."""
    stub = tmp_path / "stub.mbtiles"; stub.write_bytes(b"\x00")
    conn = sqlite3.connect(app_env)
    try:
        conn.execute(
            """UPDATE projects SET primary_mbtiles=?,
               secondary_mbtiles=NULL, tertiary_mbtiles=NULL,
               ref_mask_primary_mbtiles=NULL, ref_mask_secondary_mbtiles=NULL""",
            (str(stub),),
        )
        # Inject an action_log row pointing at a non-existent tile.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO action_log(user_id, tile_id, action, created_at)
               VALUES (1, 99999, 'classify', datetime('now'))"""
        )
        conn.commit()
    finally:
        conn.close()
    code = _run_cli("backend.scripts.verify_db", ["--quick"])
    assert code == 2
