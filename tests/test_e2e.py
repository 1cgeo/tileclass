"""End-to-end flows covering the full tile lifecycle across roles."""
import time
import jwt as pyjwt
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1, overrides=None):
    arr = np.full(65536, fill, dtype=np.uint8)
    if overrides:
        for idx, val in overrides.items():
            arr[idx] = val
    return arr.tobytes()


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


# ---------- Full operator & review lifecycle ----------

def test_full_operator_flow_auto_next(client, operators, tiles):
    """login → /next → /classify → /next returns a DIFFERENT tile (auto-advance)."""
    t = token(client, "op1", "secret123")
    tile1 = client.get("/api/tiles/next", headers=h(t)).json()
    assert tile1["status"] == "in_progress"
    assert tile1["assigned_to"] == operators[0]["id"]

    assert _classify(client, t, tile1["id"]).status_code == 200

    tile2 = client.get("/api/tiles/next", headers=h(t)).json()
    assert tile2["id"] != tile1["id"]
    assert tile2["status"] == "in_progress"


def test_full_classify_review_flow(client, operators, tiles):
    """op1 classifies (fill=1) → op2 reviews (fill=2) → reviewed mask overwrites classified."""
    from backend.mask_utils import decode_mask
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    assert _classify(client, t1, tile["id"], fill=1).status_code == 200

    # After classify: data_png should decode to all 1s
    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t1)).content
    assert decode_mask(img) == bytes([1]) * 65536

    t2 = token(client, "op2", "secret123")
    rev = client.get("/api/tiles/next", headers=h(t2)).json()
    assert rev["id"] == tile["id"]
    assert rev["status"] == "in_review"
    assert rev["classified_by"] == operators[0]["id"]
    assert rev["assigned_to"] == operators[1]["id"]

    assert _review(client, t2, tile["id"], fill=2).status_code == 200
    final = client.get(f"/api/tiles/{tile['id']}", headers=h(t2)).json()
    assert final["status"] == "reviewed"
    assert final["reviewed_by"] == operators[1]["id"]
    assert final["classified_by"] == operators[0]["id"]  # classifier preserved
    assert final["assigned_to"] is None

    # Review must overwrite the classified mask
    img2 = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t2)).content
    assert decode_mask(img2) == bytes([2]) * 65536


def test_resume_returns_same_in_progress_tile(client, operators, tiles):
    """Resume must NOT consume a new pending tile from the queue."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()

    # Pending count snapshot
    conn = connect()
    try:
        pending_before = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE status='pending'").fetchone()["c"]
    finally:
        conn.close()
    assert pending_before == 9  # 10 fixture tiles - 1 in_progress

    # Same user hits /next three more times (simulating F5 / polling)
    for _ in range(3):
        again = client.get("/api/tiles/next", headers=h(t)).json()
        assert again["id"] == tile["id"]
        assert again["status"] == "in_progress"

    conn = connect()
    try:
        pending_after = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE status='pending'").fetchone()["c"]
        in_progress = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE status='in_progress'").fetchone()["c"]
    finally:
        conn.close()
    assert pending_after == 9
    assert in_progress == 1  # no extra tile assigned


def test_resume_in_review_returned_before_new_tiles(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])

    t2 = token(client, "op2", "secret123")
    rev = client.get("/api/tiles/next", headers=h(t2)).json()
    # re-issue token mid-session: resume should bring the in_review tile back
    t2b = token(client, "op2", "secret123")
    again = client.get("/api/tiles/next", headers=h(t2b)).json()
    assert again["id"] == rev["id"]
    assert again["status"] == "in_review"


# ---------- Problem & admin recovery ----------

def test_problem_report_then_admin_reset(client, admin_user, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": "nuvens cobrem a área"})
    assert r.status_code == 200

    adm = token(client, "admin", "admin123")
    problems = client.get("/api/admin/tiles/problems", headers=h(adm)).json()
    assert any(p["id"] == tile["id"] for p in problems)

    r = client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))
    assert r.status_code == 200

    fresh = client.get(f"/api/tiles/{tile['id']}", headers=h(adm)).json()
    assert fresh["status"] == "pending"
    assert fresh["assigned_to"] is None


def test_report_problem_requires_assignment(client, operators, tiles):
    """A user who isn't assigned cannot report_problem → 403."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    t2 = token(client, "op2", "secret123")
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t2), json={"note": "not mine"})
    assert r.status_code == 403


# ---------- Re-review ----------

def test_admin_re_review_single_flow(client, admin_user, operators, tiles):
    """classify → review → admin re-review → next reviewer receives it again."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])

    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/re-review", headers=h(adm))
    assert r.status_code == 200

    state = client.get(f"/api/tiles/{tile['id']}", headers=h(adm)).json()
    assert state["status"] == "classified"
    assert state["reviewed_by"] is None

    # op3 (fresh) should receive it on /next as in_review again
    t3 = token(client, "op3", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t3)).json()
    assert nxt["id"] == tile["id"]
    assert nxt["status"] == "in_review"


def test_admin_bulk_re_review_many(client, admin_user, operators, tiles):
    reviewed_ids = []
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")
    for _ in range(3):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        _classify(client, t1, tile["id"])
        client.get("/api/tiles/next", headers=h(t2))
        _review(client, t2, tile["id"])
        reviewed_ids.append(tile["id"])

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/re-review",
                    headers=h(adm), json={"ids": reviewed_ids})
    assert r.status_code == 200
    assert r.json()["affected"] == 3

    for tid in reviewed_ids:
        state = client.get(f"/api/tiles/{tid}", headers=h(adm)).json()
        assert state["status"] == "classified"


def test_admin_bulk_reset_multiple(client, admin_user, operators, tiles):
    """Bulk reset clears status, all user fks, AND replaces data_png with empty mask."""
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    ids = []
    for _ in range(3):
        tile = client.get("/api/tiles/next", headers=h(t)).json()
        _classify(client, t, tile["id"], fill=5)
        ids.append(tile["id"])

    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/bulk/reset", headers=h(adm), json={"ids": ids})
    assert r.status_code == 200
    assert r.json()["affected"] == 3
    for tid in ids:
        state = client.get(f"/api/tiles/{tid}", headers=h(adm)).json()
        assert state["status"] == "pending"
        assert state["classified_by"] is None
        assert state["assigned_to"] is None
        img = client.get(f"/api/tiles/{tid}/image", headers=h(adm)).content
        assert decode_mask(img) == bytes([255]) * 65536  # data_png wiped

    # Reset tiles must be requeuable (no orphan lock)
    op_tok = token(client, "op2", "secret123")
    # op2 has no assignment yet → /next should return one of the reset tiles
    nxt = client.get("/api/tiles/next", headers=h(op_tok)).json()
    assert nxt["status"] == "in_progress"


# ---------- Mask validation ----------

def test_classify_rejects_invalid_class_zero(client, operators, tiles):
    """Rejection must NOT move the tile forward nor store partial data."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    raw = _mask(1, overrides={42: 0})
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"}, content=raw)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_mask"

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, classified_by, classified_at FROM tiles WHERE id=?",
            (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_progress"
    assert row["classified_by"] is None
    assert row["classified_at"] is None


def test_classify_rejects_invalid_class_seven(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    raw = _mask(1, overrides={100: 7})
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"}, content=raw)
    assert r.status_code == 400


def test_classify_wrong_size_returns_422(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=b"\x01" * 100)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_mask_size"


def test_classify_missing_count_reported(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    raw = _mask(1, overrides={0: 255, 1: 255, 2: 255, 3: 255})
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"}, content=raw)
    assert r.status_code == 422
    assert r.json()["detail"]["missing"] == 4


# ---------- Authorization ----------

def test_classify_not_assignee_forbidden(client, operators, tiles):
    """403 AND the tile state must be untouched (no assignment hijack, no data write)."""
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    t2 = token(client, "op2", "secret123")
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t2), "Content-Type": "application/octet-stream"},
                    content=_mask(3))
    assert r.status_code == 403

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, assigned_to, classified_by FROM tiles WHERE id=?",
            (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_progress"
    assert row["assigned_to"] == operators[0]["id"]
    assert row["classified_by"] is None


def test_double_submit_does_not_overwrite(client, operators, tiles):
    """Second submit is rejected AND does not overwrite the stored mask."""
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    assert _classify(client, t, tile["id"], fill=4).status_code == 200

    # Second attempt with different data
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=_mask(6))
    assert r.status_code in (403, 409)

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t)).content
    assert decode_mask(img) == bytes([4]) * 65536  # first submission preserved


def test_invalid_jwt_401(client, admin_user):
    r = client.get("/api/tiles/next", headers={"Authorization": "Bearer not.a.valid.jwt"})
    assert r.status_code == 401


def test_missing_auth_header_401(client):
    assert client.get("/api/tiles/next").status_code == 401
    assert client.get("/api/admin/dashboard").status_code == 401


def test_expired_access_token_401(client, admin_user):
    """Manually-forged expired token → 401."""
    from backend import auth as authmod
    secret = authmod.get_config()["auth"]["jwt_secret"]
    payload = {"sub": "1", "username": "admin", "role": "admin",
               "typ": "access", "exp": int(time.time()) - 10}
    expired = pyjwt.encode(payload, secret, algorithm="HS256")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_refresh_with_access_token_rejected(client, admin_user):
    """An access token must not be accepted by /refresh (typ check)."""
    t = token(client, "admin", "admin123")
    r = client.post("/api/auth/refresh", json={"refresh_token": t})
    assert r.status_code == 401


def test_refresh_fails_after_deactivation(client, admin_user, operators):
    login = client.post("/api/auth/login",
                        json={"username": "op1", "password": "secret123"}).json()
    adm = token(client, "admin", "admin123")
    client.patch(f"/api/admin/users/{operators[0]['id']}/active",
                 headers=h(adm), json={"active": False})
    r = client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert r.status_code == 401


def test_rate_limit_login_after_5_attempts(client, admin_user):
    """5 tentativas OK (status 200/401), 6ª bloqueia com 429, reset libera de novo."""
    from backend import auth as authmod
    authmod.reset_rate_limits()
    codes = []
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        codes.append(r.status_code)
    assert codes == [401] * 5  # none of the first 5 are throttled

    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 429

    # Reset must truly restore the counter (not just return a fake 429 everywhere)
    authmod.reset_rate_limits()
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r2.status_code == 200


# ---------- History, stats, image endpoints ----------

def test_tile_history_records_full_lifecycle(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    hist = client.get(f"/api/tiles/{tile['id']}/history", headers=h(t1)).json()
    actions = [row["action"] for row in hist]
    # Chronological order matters (audit trail)
    assert actions == ["assign_classify", "classify", "assign_review", "review"]
    ids = [row["id"] for row in hist]
    assert ids == sorted(ids)  # strictly increasing PK → true chronological
    by_action = {row["action"]: row for row in hist}
    assert by_action["classify"]["username"] == "op1"
    assert by_action["classify"]["user_id"] == operators[0]["id"]
    assert by_action["review"]["username"] == "op2"
    assert by_action["review"]["user_id"] == operators[1]["id"]


def test_stats_today_increments_after_classify(client, operators, tiles):
    t = token(client, "op1", "secret123")
    s0 = client.get("/api/me/stats-today", headers=h(t)).json()["count"]
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    _classify(client, t, tile["id"])
    s1 = client.get("/api/me/stats-today", headers=h(t)).json()["count"]
    assert s1 == s0 + 1


def test_queue_stats_reflects_progress(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    stats = client.get("/api/tiles/queue-stats", headers=h(t1)).json()
    assert stats["total"] == 10
    assert stats["reviewed"] == 1
    assert stats["classified"] >= 1


def test_tile_image_roundtrip_preserves_mask(client, operators, tiles):
    """Submit a non-uniform mask, fetch image, decode → bytes must match exactly."""
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()

    arr = np.tile(np.array([1, 2, 3, 4, 5, 6], dtype=np.uint8),
                  65536 // 6 + 1)[:65536]
    payload = arr.tobytes()
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=payload)
    assert r.status_code == 200

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t))
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert decode_mask(img.content) == payload


def test_preview_idempotent_and_no_state_change(client, operators, tiles):
    """N previews in a row: same id, zero rows touched. Then /next matches."""
    from backend.database import connect
    t = token(client, "op1", "secret123")
    ids = [client.get("/api/tiles/next-preview", headers=h(t)).json()["id"]
           for _ in range(4)]
    assert len(set(ids)) == 1

    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE status!='pending'").fetchone()
    finally:
        conn.close()
    assert row["c"] == 0  # no tile was assigned by preview

    nxt = client.get("/api/tiles/next", headers=h(t)).json()
    assert nxt["id"] == ids[0]


# ---------- Edge: empty queue ----------

def test_next_returns_204_when_queue_empty(client, operators):
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/next", headers=h(t))
    assert r.status_code == 204


def test_admin_create_user_then_login(client, admin_user):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/users", headers=h(adm),
                    json={"username": "newop", "password": "pw123456", "role": "operator"})
    assert r.status_code == 200
    from backend import auth as authmod
    authmod.reset_rate_limits()
    r2 = client.post("/api/auth/login",
                     json={"username": "newop", "password": "pw123456"})
    assert r2.status_code == 200
