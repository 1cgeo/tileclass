import numpy as np
from tests.conftest import token


def headers(t): return {"Authorization": f"Bearer {t}"}


# `test_dashboard` (tautológico — só espelhava a fixture) foi removido;
# `test_dashboard_real.py::test_dashboard_reflects_real_actions` cobre o caminho
# de verdade (atua antes de checar).


def test_bulk_reset(client, admin_user, operators, tiles):
    # op1 classifies tile 1
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(tok), "Content-Type": "application/octet-stream"}, content=raw)

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/reset", headers=headers(adm),
                    json={"ids": [tile["id"]]})
    assert r.status_code == 200
    assert r.json()["affected"] == 1
    tiles_list = client.get(f"/api/admin/tiles?status=pending", headers=headers(adm)).json()
    assert any(t["id"] == tile["id"] for t in tiles_list)


def test_deactivate_user_blocks_login(client, admin_user, operators):
    adm = token(client, "admin", "admin123")
    r = client.patch(f"/api/admin/users/{operators[0]['id']}/active",
                     headers=headers(adm), json={"active": False})
    assert r.status_code == 200
    # op1 can't log in anymore
    r2 = client.post("/api/auth/login", json={"username": "op1", "password": "secret123"})
    assert r2.status_code == 401


def _db_row(tile_id):
    from backend.database import connect
    conn = connect()
    try:
        return dict(conn.execute(
            "SELECT status, assigned_to, data_png, version FROM tiles WHERE id=?",
            (tile_id,)
        ).fetchone())
    finally:
        conn.close()


def test_unassign_in_progress_releases_without_resetting_mask(
    client, admin_user, operators, tiles
):
    """in_progress -> pending, assigned_to cleared, data_png untouched, version bumped."""
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    before = _db_row(tile["id"])
    assert before["status"] == "in_progress"
    assert before["assigned_to"] == operators[0]["id"]

    adm = token(client, "admin", "admin123")
    r = client.post(
        f"/api/admin/tiles/{tile['id']}/unassign",
        headers=headers(adm), json={"reason": "operator on vacation"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": tile["id"], "status": "pending"}

    after = _db_row(tile["id"])
    assert after["status"] == "pending"
    assert after["assigned_to"] is None
    # Mask bytes are unchanged (reset would have rewritten; unassign must not).
    assert after["data_png"] == before["data_png"]
    assert after["version"] == before["version"] + 1


def test_unassign_in_review_preserves_classified_mask(
    client, admin_user, operators, tiles
):
    """Critical case: a classified tile picked up for review must keep its mask
    when the reviewer is released. reset_tile would wipe it; unassign must not."""
    import numpy as np
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    painted = np.full(65536, 3, dtype=np.uint8).tobytes()  # class 3
    r = client.post(
        f"/api/tiles/{tile['id']}/classify",
        headers={**headers(op1), "Content-Type": "application/octet-stream"},
        content=painted,
    )
    assert r.status_code == 200, r.text

    # op2 pulls it for review -> in_review
    op2 = token(client, "op2", "secret123")
    reviewed = client.get("/api/tiles/next", headers=headers(op2)).json()
    assert reviewed["id"] == tile["id"]
    before = _db_row(tile["id"])
    assert before["status"] == "in_review"
    assert before["assigned_to"] == operators[1]["id"]

    adm = token(client, "admin", "admin123")
    r = client.post(
        f"/api/admin/tiles/{tile['id']}/unassign",
        headers=headers(adm), json={"reason": "reviewer AFK"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "classified"

    after = _db_row(tile["id"])
    assert after["status"] == "classified"
    assert after["assigned_to"] is None
    assert after["data_png"] == before["data_png"]  # classified mask intact


def test_unassign_rejects_pending_tile(client, admin_user, tiles):
    """No operator is assigned on pending tiles; 409 is the right signal."""
    adm = token(client, "admin", "admin123")
    # pick a freshly inserted pending tile
    tile_id = client.get("/api/admin/tiles?status=pending",
                         headers=headers(adm)).json()[0]["id"]
    r = client.post(f"/api/admin/tiles/{tile_id}/unassign", headers=headers(adm),
                    json={"reason": "x"})
    assert r.status_code == 409


def test_unassign_rejects_reviewed_tile(client, admin_user, operators, tiles):
    import numpy as np
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 2, dtype=np.uint8).tobytes())
    op2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=headers(op2))
    client.post(f"/api/tiles/{tile['id']}/review",
                headers={**headers(op2), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 2, dtype=np.uint8).tobytes())
    assert _db_row(tile["id"])["status"] == "reviewed"

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/unassign",
                    headers=headers(adm), json={})
    assert r.status_code == 409


def test_unassign_returns_tile_to_queue(client, admin_user, operators, tiles):
    """After unassign, the same tile can be served again by /next — to the same or a different operator."""
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/unassign",
                headers=headers(adm), json={})

    # op1 no longer has a resume; op2 may now pick it up.
    op2 = token(client, "op2", "secret123")
    served = client.get("/api/tiles/next", headers=headers(op2)).json()
    assert served["id"] == tile["id"]
    assert _db_row(tile["id"])["status"] == "in_progress"


def test_unassign_logs_action_with_reason(client, admin_user, operators, tiles):
    from backend.database import connect
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/unassign",
                headers=headers(adm), json={"reason": "férias"})
    conn = connect()
    try:
        row = conn.execute(
            "SELECT user_id, action, detail FROM action_log "
            "WHERE tile_id=? AND action='unassign' ORDER BY id DESC LIMIT 1",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["user_id"] == admin_user["id"]
    assert row["detail"] == "férias"


def test_unassign_requires_admin(client, admin_user, operators, tiles):
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    # op1 cannot unassign himself via the admin endpoint
    r = client.post(f"/api/admin/tiles/{tile['id']}/unassign", headers=headers(op1),
                    json={"reason": "x"})
    assert r.status_code == 403


def test_bulk_unassign_releases_in_progress_and_in_review(
    client, admin_user, operators, tiles
):
    """Bulk unassign mirrors the single-tile transitions: in_progress→pending
    and in_review→classified, both preserving the mask."""
    op1 = token(client, "op1", "secret123")
    op2 = token(client, "op2", "secret123")
    op3 = token(client, "op3", "secret123")

    # op1 picks tile A first so the review queue is empty when op3 asks for
    # work — otherwise op1 (can_review=1) would grab op3's tile for review
    # before op3 has a chance to classify a different one.
    a = client.get("/api/tiles/next", headers=headers(op1)).json()
    assert a["status"] == "in_progress"

    # op3 grabs the next pending tile and classifies it.
    b = client.get("/api/tiles/next", headers=headers(op3)).json()
    assert b["id"] != a["id"]
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    client.post(f"/api/tiles/{b['id']}/classify",
                headers={**headers(op3), "Content-Type": "application/octet-stream"}, content=raw)

    # op2 picks tile B for review (op3 ≠ op2, so reviewer eligibility holds).
    in_review = client.get("/api/tiles/next", headers=headers(op2)).json()
    assert in_review["id"] == b["id"]
    assert in_review["status"] == "in_review"

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/unassign", headers=headers(adm),
                    json={"ids": [a["id"], b["id"]], "reason": "shift over"})
    assert r.status_code == 200
    assert r.json()["affected"] == 2

    after_a = _db_row(a["id"])
    after_b = _db_row(b["id"])
    assert after_a["status"] == "pending"
    assert after_a["assigned_to"] is None
    assert after_b["status"] == "classified"
    assert after_b["assigned_to"] is None
    # Classified mask survived the reviewer release.
    assert after_b["data_png"] is not None


def test_bulk_unassign_is_atomic_on_invalid_state(
    client, admin_user, operators, tiles
):
    """A single unassignable tile in the batch (e.g. pending) must abort the
    whole call — admins shouldn't get a partial result they can't reason about.
    Mirrors the contract of bulk/block."""
    op1 = token(client, "op1", "secret123")
    a = client.get("/api/tiles/next", headers=headers(op1)).json()  # in_progress
    adm = token(client, "admin", "admin123")
    pending_id = next(
        t["id"] for t in client.get("/api/admin/tiles?status=pending",
                                    headers=headers(adm)).json()
        if t["id"] != a["id"]
    )
    r = client.post("/api/admin/tiles/bulk/unassign", headers=headers(adm),
                    json={"ids": [a["id"], pending_id]})
    assert r.status_code == 409
    # `a` must still be in_progress — atomic rollback.
    assert _db_row(a["id"])["status"] == "in_progress"


def test_bulk_unassign_requires_admin(
    client, admin_user, operators, tiles
):
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    r = client.post("/api/admin/tiles/bulk/unassign", headers=headers(op1),
                    json={"ids": [tile["id"]]})
    assert r.status_code == 403


def test_list_tiles_exposes_current_assignee(
    client, admin_user, operators, tiles
):
    """The admin tiles listing must expose the currently-assigned operator
    so the dashboard can show who is working on in_progress / in_review tiles,
    not only completed work."""
    op1 = token(client, "op1", "secret123")
    assigned = client.get("/api/tiles/next", headers=headers(op1)).json()
    adm = token(client, "admin", "admin123")
    listing = client.get("/api/admin/tiles?status=in_progress",
                         headers=headers(adm)).json()
    row = next(r for r in listing if r["id"] == assigned["id"])
    assert row["assigned_to"] == operators[0]["id"]
    assert row["assigned_to_username"] == "op1"
    # Not yet classified -> classified_by_username stays empty so the frontend
    # can distinguish "currently assigned" from "completed by".
    assert row["classified_by_username"] is None


def _set_can_review(client, admin_tok, user_id, flag):
    return client.patch(f"/api/admin/users/{user_id}/can-review",
                        headers=headers(admin_tok), json={"can_review": flag})


def test_operator_without_can_review_skips_review_queue(
    client, admin_user, operators, tiles
):
    """An operator not opted-in as reviewer must never be assigned a classified
    tile from the review queue — even if one is available. They fall through to
    the pending queue instead."""
    import numpy as np
    adm = token(client, "admin", "admin123")
    # Revoke op2 so we can assert the filter blocks them.
    _set_can_review(client, adm, operators[1]["id"], False)

    op1 = token(client, "op1", "secret123")
    tile1 = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile1['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())

    op2 = token(client, "op2", "secret123")
    served = client.get("/api/tiles/next", headers=headers(op2)).json()
    assert served["id"] != tile1["id"]
    assert served["status"] == "in_progress"


def test_operator_with_can_review_gets_review_tile_first(
    client, admin_user, operators, tiles
):
    """Happy path: opted-in operator is assigned the review queue before pending."""
    import numpy as np
    adm = token(client, "admin", "admin123")
    # Revoke then re-grant to also exercise the endpoint's on path.
    _set_can_review(client, adm, operators[1]["id"], False)

    op1 = token(client, "op1", "secret123")
    tile1 = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile1['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())

    r = _set_can_review(client, adm, operators[1]["id"], True)
    assert r.status_code == 200 and r.json()["can_review"] is True

    op2 = token(client, "op2", "secret123")
    served = client.get("/api/tiles/next", headers=headers(op2)).json()
    assert served["id"] == tile1["id"]
    assert served["status"] == "in_review"


def test_revoking_can_review_blocks_future_review_assignments(
    client, admin_user, operators, tiles
):
    import numpy as np
    op1 = token(client, "op1", "secret123")
    tile1 = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile1['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())

    adm = token(client, "admin", "admin123")
    # Opt-out: was defaulted on by the fixture; admin turns it off.
    _set_can_review(client, adm, operators[1]["id"], False)

    op2 = token(client, "op2", "secret123")
    served = client.get("/api/tiles/next", headers=headers(op2)).json()
    # Opt-out effective immediately: tile1 still 'classified' in the DB,
    # but op2 gets a pending one.
    assert served["id"] != tile1["id"]
    assert served["status"] == "in_progress"


def test_list_users_exposes_can_review_flag(client, admin_user, operators):
    """The users endpoint must surface can_review so the admin UI can render
    and toggle it. Default value is not asserted here (the fixture forces on);
    what matters is that the column exists in the payload."""
    adm = token(client, "admin", "admin123")
    users = client.get("/api/admin/users", headers=headers(adm)).json()
    for u in users:
        assert "can_review" in u, f"user row missing can_review: {u}"


def test_can_review_endpoint_requires_admin(client, admin_user, operators):
    op1 = token(client, "op1", "secret123")
    r = client.patch(f"/api/admin/users/{operators[1]['id']}/can-review",
                     headers=headers(op1), json={"can_review": True})
    assert r.status_code == 403


def _set_role(client, admin_tok, user_id, role):
    return client.patch(f"/api/admin/users/{user_id}/role",
                        headers=headers(admin_tok), json={"role": role})


def test_promote_operator_to_admin_grants_review_rights(
    client, admin_user, operators
):
    adm = token(client, "admin", "admin123")
    # operator starts with can_review=0 (fixture forces it on, normalize first)
    client.patch(f"/api/admin/users/{operators[0]['id']}/can-review",
                 headers=headers(adm), json={"can_review": False})
    r = _set_role(client, adm, operators[0]["id"], "admin")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": operators[0]["id"], "role": "admin"}
    # The promoted user should now show up as admin AND as a reviewer.
    users = {u["id"]: u for u in client.get("/api/admin/users",
                                            headers=headers(adm)).json()}
    assert users[operators[0]["id"]]["role"] == "admin"
    assert bool(users[operators[0]["id"]]["can_review"]) is True


def test_demote_admin_to_operator(client, admin_user, operators):
    adm = token(client, "admin", "admin123")
    # First create a second admin so we have someone to demote.
    _set_role(client, adm, operators[0]["id"], "admin")
    r = _set_role(client, adm, operators[0]["id"], "operator")
    assert r.status_code == 200
    assert r.json()["role"] == "operator"


def test_cannot_demote_last_active_admin(client, admin_user, operators):
    adm = token(client, "admin", "admin123")
    # admin_user fixture creates a single admin; demoting it must fail.
    me = client.get("/api/auth/me", headers=headers(adm)).json()
    r = _set_role(client, adm, me["id"], "operator")
    assert r.status_code == 409
    assert "last active admin" in r.json()["detail"].lower()


def test_set_role_rejects_invalid_value(client, admin_user, operators):
    adm = token(client, "admin", "admin123")
    r = client.patch(f"/api/admin/users/{operators[0]['id']}/role",
                     headers=headers(adm), json={"role": "superuser"})
    # Pydantic Literal -> 422.
    assert r.status_code == 422


def test_set_role_endpoint_requires_admin(client, admin_user, operators):
    op1 = token(client, "op1", "secret123")
    r = client.patch(f"/api/admin/users/{operators[1]['id']}/role",
                     headers=headers(op1), json={"role": "admin"})
    assert r.status_code == 403


def test_create_admin_user_seeds_can_review(client, admin_user):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/users", headers=headers(adm),
                    json={"username": "admin2", "password": "secret123",
                          "role": "admin"})
    assert r.status_code == 200
    users = {u["username"]: u for u in client.get("/api/admin/users",
                                                  headers=headers(adm)).json()}
    assert users["admin2"]["role"] == "admin"
    assert bool(users["admin2"]["can_review"]) is True


def test_admin_assigns_pending_tile_to_operator(
    client, admin_user, operators, tiles
):
    """Admin hand-picks an operator for a pending tile. The tile becomes
    in_progress / assigned_to that operator, and the standard assign_classify
    action is logged under the operator (so dashboard pairing still works)."""
    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending",
                         headers=headers(adm)).json()
    tile_id = pending[0]["id"]

    r = client.post(f"/api/admin/tiles/{tile_id}/assign", headers=headers(adm),
                    json={"user_id": operators[2]["id"], "reason": "urgent batch"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": tile_id, "status": "in_progress",
                    "assigned_to": operators[2]["id"]}

    # op3 resumes this tile on next login.
    tok = token(client, "op3", "secret123")
    mine = client.get("/api/tiles/assigned", headers=headers(tok)).json()
    assert mine["id"] == tile_id

    # assign_classify is logged under the operator (not the admin) to keep
    # dashboard avg_classify_seconds pairing assign→classify intact.
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT user_id FROM action_log "
            "WHERE tile_id=? AND action='assign_classify' ORDER BY id DESC LIMIT 1",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["user_id"] == operators[2]["id"]


def test_admin_assigns_reviewer_to_classified_tile(
    client, admin_user, operators, tiles
):
    import numpy as np
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/assign", headers=headers(adm),
                    json={"user_id": operators[1]["id"]})
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"


def test_admin_assign_rejects_classifier_as_reviewer(
    client, admin_user, operators, tiles
):
    """The classifier of a tile must never be assigned to review it."""
    import numpy as np
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/assign", headers=headers(adm),
                    json={"user_id": operators[0]["id"]})
    assert r.status_code == 409


def test_admin_assign_reviewer_must_have_can_review(
    client, admin_user, operators, tiles
):
    import numpy as np
    adm = token(client, "admin", "admin123")
    _set_can_review(client, adm, operators[1]["id"], False)

    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())

    r = client.post(f"/api/admin/tiles/{tile['id']}/assign", headers=headers(adm),
                    json={"user_id": operators[1]["id"]})
    assert r.status_code == 409


def test_admin_assign_rejects_in_progress_tile(
    client, admin_user, operators, tiles
):
    """Cannot re-assign a tile that is already assigned — admin must
    unassign first. Prevents silent work loss."""
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/assign", headers=headers(adm),
                    json={"user_id": operators[1]["id"]})
    assert r.status_code == 409


def test_admin_assign_rejects_inactive_user(
    client, admin_user, operators, tiles
):
    adm = token(client, "admin", "admin123")
    client.patch(f"/api/admin/users/{operators[0]['id']}/active",
                 headers=headers(adm), json={"active": False})
    pending_id = client.get("/api/admin/tiles?status=pending",
                            headers=headers(adm)).json()[0]["id"]
    r = client.post(f"/api/admin/tiles/{pending_id}/assign", headers=headers(adm),
                    json={"user_id": operators[0]["id"]})
    assert r.status_code == 409


def test_admin_assign_requires_admin_role(client, admin_user, operators, tiles):
    op1 = token(client, "op1", "secret123")
    pending_id = client.get("/api/admin/tiles?status=pending",
                            headers=headers(op1)).json() if False else None
    # operators cannot list admin tiles either; hit the assign endpoint directly.
    r = client.post(f"/api/admin/tiles/1/assign", headers=headers(op1),
                    json={"user_id": operators[1]["id"]})
    assert r.status_code == 403


# ---------- Admin pause (operator forgot to pause before leaving) ----------

def _last_action(tile_id: int, action: str):
    from backend.database import connect
    conn = connect()
    try:
        return conn.execute(
            "SELECT user_id, detail FROM action_log "
            "WHERE tile_id=? AND action=? ORDER BY id DESC LIMIT 1",
            (tile_id, action),
        ).fetchone()
    finally:
        conn.close()


def test_admin_pause_in_progress_sets_paused_at(client, admin_user, operators, tiles):
    """Happy path: admin pauses an in_progress tile. Tile stays assigned and
    in_progress, paused_at is set, version bumps."""
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    before = _db_row(tile["id"])
    assert before["status"] == "in_progress"

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(adm), json={"reason": "foi embora sem pausar"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": tile["id"], "status": "in_progress", "paused": True}

    from backend.database import connect
    conn = connect()
    try:
        row = dict(conn.execute(
            "SELECT status, assigned_to, paused_at, version FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone())
    finally:
        conn.close()
    assert row["status"] == "in_progress"
    assert row["assigned_to"] == operators[0]["id"]  # still assigned
    assert row["paused_at"] is not None
    assert row["version"] == before["version"] + 1


def test_admin_pause_in_review_keeps_reviewer_assigned(
    client, admin_user, operators, tiles
):
    """in_review is also pauseable; reviewer remains the assignee."""
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())
    op2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=headers(op2))
    assert _db_row(tile["id"])["status"] == "in_review"
    assert _db_row(tile["id"])["assigned_to"] == operators[1]["id"]

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(adm), json={})
    assert r.status_code == 200, r.text
    assert _db_row(tile["id"])["assigned_to"] == operators[1]["id"]


def test_admin_pause_logs_under_operator_and_admin(
    client, admin_user, operators, tiles
):
    """The `pause` log must be attributed to the operator (so _cycle_durations
    pairs it against their assign→classify cycle). A separate `admin_pause`
    entry under the admin provides audit trail."""
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                headers=headers(adm), json={"reason": "AFK"})

    pause = _last_action(tile["id"], "pause")
    assert pause is not None
    assert pause["user_id"] == operators[0]["id"]
    assert pause["detail"] == "admin"  # not 'queue', so /next won't auto-resume

    audit = _last_action(tile["id"], "admin_pause")
    assert audit is not None
    assert audit["user_id"] == admin_user["id"]
    assert "AFK" in (audit["detail"] or "")


def test_admin_pause_rejects_pending_tile(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    tid = client.get("/api/admin/tiles?status=pending",
                     headers=headers(adm)).json()[0]["id"]
    r = client.post(f"/api/admin/tiles/{tid}/admin-pause",
                    headers=headers(adm), json={})
    assert r.status_code == 409


def test_admin_pause_rejects_classified_tile(
    client, admin_user, operators, tiles
):
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(op1), "Content-Type": "application/octet-stream"},
                content=np.full(65536, 1, dtype=np.uint8).tobytes())
    assert _db_row(tile["id"])["status"] == "classified"
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(adm), json={})
    assert r.status_code == 409


def test_admin_pause_rejects_already_paused(client, admin_user, operators, tiles):
    """Second admin-pause call is a no-op with a clear 409, so an accidental
    double-click doesn't bury the original paused_at timestamp."""
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(adm), json={})
    assert r.status_code == 200
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(adm), json={})
    assert r.status_code == 409


def test_admin_pause_requires_admin(client, admin_user, operators, tiles):
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(op1)).json()
    r = client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                    headers=headers(op1), json={})
    assert r.status_code == 403


def test_admin_pause_is_not_auto_resumed_by_next(
    client, admin_user, operators, tiles
):
    """When the operator comes back and hits /next, an admin-pause must NOT
    auto-resume (detail != 'queue'). Timer stays stopped until they explicitly
    resume via editor."""
    tok = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(tok)).json()
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/admin-pause",
                headers=headers(adm), json={})

    r = client.get("/api/tiles/next", headers=headers(tok))
    assert r.status_code == 200
    assert r.json()["id"] == tile["id"]
    # Still paused — /next did not clear paused_at.
    assert _db_row(tile["id"])
    from backend.database import connect
    conn = connect()
    try:
        row = dict(conn.execute(
            "SELECT paused_at FROM tiles WHERE id=?", (tile["id"],)
        ).fetchone())
    finally:
        conn.close()
    assert row["paused_at"] is not None


def test_thumbnail(client, admin_user, tiles):
    """Thumbnail must be a valid PNG of the requested size, not arbitrary bytes."""
    import io
    from PIL import Image
    t = token(client, "admin", "admin123")
    tiles_list = client.get("/api/admin/tiles", headers=headers(t)).json()
    tid = tiles_list[0]["id"]
    r = client.get(f"/api/admin/tiles/{tid}/thumbnail?size=64", headers=headers(t))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(r.content))
    assert img.format == "PNG"
    assert img.size == (64, 64)


def test_thumbnail_falls_back_to_blank_on_missing_blob(client, admin_user, tiles):
    """A tile whose data_png is NULL (or corrupt) must still return a 200 PNG,
    rendered as the empty mask — the admin grid should never 422 on rows that
    simply haven't been painted yet."""
    import io
    from PIL import Image
    from backend.database import connect
    t = token(client, "admin", "admin123")
    tid = client.get("/api/admin/tiles", headers=headers(t)).json()[0]["id"]
    conn = connect()
    try:
        conn.execute("UPDATE tiles SET data_png=NULL WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/admin/tiles/{tid}/thumbnail?size=64", headers=headers(t))
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (64, 64)
