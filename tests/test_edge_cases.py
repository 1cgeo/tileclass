"""Edge cases and real-world scenarios: malformed input, boundary states, invariants."""
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def _classify(client, tok, tile_id, fill=1):
    return client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(tok), "Content-Type": "application/octet-stream"},
        content=_mask(fill),
    )


def _review(client, tok, tile_id, fill=2):
    return client.post(
        f"/api/tiles/{tile_id}/review",
        headers={**h(tok), "Content-Type": "application/octet-stream"},
        content=_mask(fill),
    )


# ---------- Unknown resource ids ----------

def test_classify_unknown_tile_returns_404(client, operators):
    t = token(client, "op1", "secret123")
    r = client.post("/api/tiles/9999/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=_mask(1))
    assert r.status_code == 404


def test_get_tile_unknown_id_404(client, operators):
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/9999", headers=h(t))
    assert r.status_code == 404


def test_get_tile_image_unknown_id_404(client, operators):
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/9999/image", headers=h(t))
    assert r.status_code == 404


def test_history_unknown_tile_returns_404(client, operators):
    """Unknown tile_id must return 404 (not 500). History is membership-gated
    by the tile's project, so a missing tile has no project to authorise
    against — same 404 contract as GET /api/tiles/{id}."""
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/9999/history", headers=h(t))
    assert r.status_code == 404


# ---------- State-machine illegal transitions ----------

def test_classify_on_unassigned_classified_tile_forbidden(client, operators, tiles):
    """After classify, tile has assigned_to=NULL. A resubmit is 403 (not 500)."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    assert _classify(client, t, tile["id"]).status_code == 200
    r = _classify(client, t, tile["id"])
    assert r.status_code == 403


def test_classify_and_review_endpoints_are_state_driven(client, operators, tiles):
    """Design: both endpoints delegate to submit_classification; status decides the transition.
    A /review call on an in_progress tile acts as classify (200, status→classified)."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = _review(client, t, tile["id"])
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute("SELECT status FROM tiles WHERE id=?", (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"


def test_report_problem_unknown_tile_404(client, operators):
    t = token(client, "op1", "secret123")
    r = client.post("/api/tiles/9999/report-problem",
                    headers=h(t), json={"note": "missing"})
    assert r.status_code == 404


def test_admin_re_review_non_reviewed_tile_is_409(client, admin_user, operators, tiles):
    """Single-wrapper uses strict=True → 409 on wrong state."""
    adm = token(client, "admin", "admin123")
    # tile is still pending
    pending_list = client.get("/api/admin/tiles?status=pending", headers=h(adm)).json()
    tid = pending_list[0]["id"]
    r = client.post(f"/api/admin/tiles/{tid}/re-review", headers=h(adm))
    assert r.status_code == 409


def test_admin_bulk_re_review_skips_non_reviewed(client, admin_user, operators, tiles):
    """Non-strict bulk: mix of reviewed + pending → affected counts only reviewed."""
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")
    # Fully review one tile
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])
    reviewed_id = tile["id"]

    adm = token(client, "admin", "admin123")
    pending = client.get("/api/admin/tiles?status=pending", headers=h(adm)).json()[0]["id"]

    r = client.post("/api/admin/tiles/bulk/re-review", headers=h(adm),
                    json={"ids": [reviewed_id, pending, 9999]})
    assert r.status_code == 200
    assert r.json()["affected"] == 1  # only the reviewed one


def test_bulk_reset_empty_list_is_zero_not_error(client, admin_user):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/reset", headers=h(adm), json={"ids": []})
    assert r.status_code == 200
    assert r.json()["affected"] == 0


# ---------- Re-review preserves data_png (unlike reset) ----------

def test_re_review_preserves_data_png(client, admin_user, operators, tiles):
    """Re-review puts tile back to 'classified' but keeps the stored mask for the reviewer."""
    from backend.mask_utils import decode_mask
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"], fill=3)
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"], fill=5)

    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/re-review", headers=h(adm))

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(adm)).content
    assert decode_mask(img) == bytes([5]) * 65536  # reviewer's mask preserved


# ---------- Queue behavior ----------

def test_problem_tile_never_returned_by_next(client, operators, tiles):
    """Report problem → that tile must be excluded from /next for ALL users."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    client.post(f"/api/tiles/{tile['id']}/report-problem",
                headers=h(t), json={"note": "foo"})

    # op1 now gets a different pending tile (problem is released from assigned_to)
    for _ in range(10):
        nxt = client.get("/api/tiles/next", headers=h(t))
        if nxt.status_code == 204:
            break
        j = nxt.json()
        assert j["id"] != tile["id"]
        _classify(client, t, j["id"])


def test_reviewed_tile_never_returned_by_next(client, operators, tiles):
    """A fully-reviewed tile is terminal — no operator gets it again."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    # Drain the queue for op3 — should never see tile['id'] again
    t3 = token(client, "op3", "secret123")
    seen = set()
    for _ in range(20):
        r = client.get("/api/tiles/next", headers=h(t3))
        if r.status_code == 204:
            break
        tid = r.json()["id"]
        assert tid != tile["id"]
        seen.add(tid)
        _classify(client, t3, tid)


def test_pending_queue_is_fifo_by_id(client, operators, tiles):
    """/next on pending drains in id-ascending order."""
    ids_served = []
    for user in ("op1", "op2", "op3"):
        tok = token(client, user, "secret123")
        r = client.get("/api/tiles/next", headers=h(tok)).json()
        ids_served.append(r["id"])
    assert ids_served == sorted(ids_served)


# ---------- Pydantic validation ----------

def test_report_problem_empty_note_rejected(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": ""})
    assert r.status_code == 422


def test_create_user_too_short_password_rejected(client, admin_user):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/users", headers=h(adm),
                    json={"username": "newop", "password": "123", "role": "operator"})
    assert r.status_code == 422


def test_create_user_invalid_role_rejected(client, admin_user):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/users", headers=h(adm),
                    json={"username": "newop", "password": "pw123456",
                          "role": "superuser"})
    assert r.status_code == 422


def test_create_user_duplicate_username_409(client, admin_user):
    adm = token(client, "admin", "admin123")
    body = {"username": "dupe", "password": "pw123456", "role": "operator"}
    assert client.post("/api/admin/users", headers=h(adm), json=body).status_code == 200
    r = client.post("/api/admin/users", headers=h(adm), json=body)
    assert r.status_code == 409


def test_refresh_malformed_token_401(client):
    r = client.post("/api/auth/refresh", json={"refresh_token": "garbage.not.jwt"})
    assert r.status_code == 401


def test_login_missing_field_422(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin"})
    assert r.status_code == 422


# ---------- Authorization boundaries ----------

def test_operator_cannot_list_users(client, operators):
    t = token(client, "op1", "secret123")
    r = client.get("/api/admin/users", headers=h(t))
    assert r.status_code == 403


def test_operator_cannot_create_user(client, operators):
    t = token(client, "op1", "secret123")
    r = client.post("/api/admin/users", headers=h(t),
                    json={"username": "x", "password": "pw123456", "role": "operator"})
    assert r.status_code == 403


def test_operator_cannot_bulk_reset(client, operators, tiles):
    t = token(client, "op1", "secret123")
    r = client.post("/api/admin/tiles/bulk/reset", headers=h(t), json={"ids": [1]})
    assert r.status_code == 403


def test_reactivate_user_allows_login_again(client, admin_user, operators):
    """Deactivate → login 401 → reactivate → login 200."""
    from backend import auth as authmod
    adm = token(client, "admin", "admin123")
    uid = operators[0]["id"]
    client.patch(f"/api/admin/users/{uid}/active", headers=h(adm), json={"active": False})
    authmod.reset_rate_limits()
    assert client.post("/api/auth/login",
                       json={"username": "op1", "password": "secret123"}).status_code == 401

    client.patch(f"/api/admin/users/{uid}/active", headers=h(adm), json={"active": True})
    authmod.reset_rate_limits()
    assert client.post("/api/auth/login",
                       json={"username": "op1", "password": "secret123"}).status_code == 200


# ---------- Admin listings & pagination ----------

def test_admin_tiles_status_filter(client, admin_user, operators, tiles):
    """status=classified returns only classified tiles."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _classify(client, t, tile["id"])

    adm = token(client, "admin", "admin123")
    classified = client.get("/api/admin/tiles?status=classified", headers=h(adm)).json()
    pending = client.get("/api/admin/tiles?status=pending", headers=h(adm)).json()
    assert [r["id"] for r in classified] == [tile["id"]]
    assert tile["id"] not in [r["id"] for r in pending]
    assert len(pending) == 9


def test_admin_tiles_pagination(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    page1 = client.get("/api/admin/tiles?limit=3&offset=0", headers=h(adm)).json()
    page2 = client.get("/api/admin/tiles?limit=3&offset=3", headers=h(adm)).json()
    assert len(page1) == 3
    assert len(page2) == 3
    ids1 = [t["id"] for t in page1]
    ids2 = [t["id"] for t in page2]
    assert set(ids1).isdisjoint(ids2)
    # Order preserved across pages
    assert sorted(ids1 + ids2) == ids1 + ids2


def test_admin_tiles_user_filter(client, admin_user, operators, tiles):
    """Filter by user_id returns only tiles that user classified or reviewed."""
    t1 = token(client, "op1", "secret123")
    tile_a = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile_a["id"])
    t2 = token(client, "op2", "secret123")
    tile_b = client.get("/api/tiles/next", headers=h(t2)).json()
    # tile_b here is the review-priority one (tile_a, since op2 isn't classifier)
    # So op2 reviews tile_a; let op2 also classify a new pending
    _review(client, t2, tile_a["id"])
    tile_c = client.get("/api/tiles/next", headers=h(t2)).json()
    _classify(client, t2, tile_c["id"])

    adm = token(client, "admin", "admin123")
    op1_tiles = client.get(f"/api/admin/tiles?user_id={operators[0]['id']}",
                           headers=h(adm)).json()
    op2_tiles = client.get(f"/api/admin/tiles?user_id={operators[1]['id']}",
                           headers=h(adm)).json()
    assert tile_a["id"] in [r["id"] for r in op1_tiles]
    assert tile_a["id"] in [r["id"] for r in op2_tiles]  # reviewed
    assert tile_c["id"] in [r["id"] for r in op2_tiles]
    assert tile_c["id"] not in [r["id"] for r in op1_tiles]


# ---------- Dashboard updates with real activity ----------

def test_dashboard_reflects_mutations(client, admin_user, operators, tiles):
    """Dashboard totals and per_operator counts reflect actual actions."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    assert d["totals_by_status"].get("reviewed") == 1
    assert d["totals_by_status"].get("pending") == 9
    by_user = {u["username"]: u for u in d["per_operator"]}
    assert by_user["op1"]["classified"] == 1
    assert by_user["op2"]["reviewed"] == 1
    assert d["completion_percent"] == 10.0  # 1 of 10


# ---------- Unicode & content integrity ----------

def test_problem_note_preserves_portuguese_unicode(client, operators, tiles):
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    note = "Área com nuvens — não é possível distinguir vegetação de água 🌧"
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": note})
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute("SELECT problem_note FROM tiles WHERE id=?",
                           (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["problem_note"] == note


def test_problem_note_at_max_length_accepted(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    note = "x" * 2000
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": note})
    assert r.status_code == 200


def test_problem_note_over_max_length_rejected(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": "x" * 2001})
    assert r.status_code == 422
