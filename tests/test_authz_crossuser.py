"""Cross-user authorization: no user can act on a tile not assigned to them."""
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def _post_mask(client, tok, path):
    return client.post(path, headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask())


# ---------- Reviewer can never review their own classification ----------

def test_operator_cannot_force_review_own_classified_tile(client, operators, tiles):
    """
    op1 classifies tile. Without passing through /next again, op1 POSTs directly
    to /review/{id}. Backend must reject — self-review bypass must not work.
    """
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    assert _post_mask(client, t1, f"/api/tiles/{tile['id']}/classify").status_code == 200

    # Tile is now status=classified, assigned_to=NULL. op1 forces /review.
    r = _post_mask(client, t1, f"/api/tiles/{tile['id']}/review")
    assert r.status_code in (403, 409), r.text

    # State must be untouched
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, reviewed_by FROM tiles WHERE id=?", (tile["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["reviewed_by"] is None


def test_next_never_assigns_own_classification_as_review(client, operators, tiles):
    """/next for op1 must skip tiles op1 previously classified, even if it's the only one."""
    t1 = token(client, "op1", "secret123")
    # Classify several tiles, leaving them in classified state
    classified_ids = []
    for _ in range(3):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        assert _post_mask(client, t1, f"/api/tiles/{tile['id']}/classify").status_code == 200
        classified_ids.append(tile["id"])

    # op1 requests /next again — must receive a *pending* tile, never a classified-by-self
    nxt = client.get("/api/tiles/next", headers=h(t1)).json()
    assert nxt["id"] not in classified_ids
    assert nxt["status"] == "in_progress"


# ---------- Cross-user submit must not hijack ----------

def test_operator_cannot_classify_tile_assigned_to_other(client, operators, tiles):
    """op2 tries to POST classify on op1's in_progress tile."""
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    t2 = token(client, "op2", "secret123")
    r = _post_mask(client, t2, f"/api/tiles/{tile['id']}/classify")
    assert r.status_code == 403

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, assigned_to, classified_by FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_progress"
    assert row["assigned_to"] == operators[0]["id"]
    assert row["classified_by"] is None


def test_operator_cannot_review_tile_not_in_review_state(client, operators, tiles):
    """Pending tile with no assignment → POST /review directly → 403 or 409."""
    t1 = token(client, "op1", "secret123")
    # Grab a pending tile id WITHOUT assigning
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM tiles WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    pending_id = row["id"]

    r = _post_mask(client, t1, f"/api/tiles/{pending_id}/review")
    assert r.status_code in (403, 409)


def test_operator_cannot_report_problem_on_other_users_tile(client, operators, tiles):
    """Replicates a known gap at assign level for report-problem."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    t2 = token(client, "op2", "secret123")
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t2), json={"note": "pwn"})
    assert r.status_code == 403


def test_operator_cannot_pause_other_users_tile(client, operators, tiles):
    """op2 pausing op1's in_progress tile → 403, paused_at untouched."""
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    t2 = token(client, "op2", "secret123")
    r = client.post(f"/api/tiles/{tile['id']}/pause",
                    headers={**h(t2), "Content-Type": "application/octet-stream"}, content=_mask(255))
    assert r.status_code == 403
    conn = connect()
    try:
        row = conn.execute("SELECT paused_at, assigned_to FROM tiles WHERE id=?", (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["paused_at"] is None and row["assigned_to"] == operators[0]["id"]


def test_operator_cannot_resume_other_users_tile(client, operators, tiles):
    """op1 pauses; op2 cannot resume it (→403)."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    client.post(f"/api/tiles/{tile['id']}/pause",
                headers={**h(t1), "Content-Type": "application/octet-stream",
                         "X-Tile-Version": str(tile["version"])}, content=_mask(255))
    t2 = token(client, "op2", "secret123")
    assert client.post(f"/api/tiles/{tile['id']}/resume", headers=h(t2)).status_code == 403


def test_operator_cannot_heartbeat_other_users_tile(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    t2 = token(client, "op2", "secret123")
    assert client.post(f"/api/tiles/{tile['id']}/heartbeat", headers=h(t2)).status_code == 403


def test_classifier_cannot_request_changes_on_review(client, operators, tiles):
    """request_changes is a reviewer action (assigned_to==reviewer). The original
    classifier (not the assigned reviewer) is rejected 403, tile stays in_review."""
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _post_mask(client, t1, f"/api/tiles/{tile['id']}/classify")
    t2 = token(client, "op2", "secret123")
    rev = client.get("/api/tiles/next", headers=h(t2)).json()
    assert rev["id"] == tile["id"] and rev["status"] == "in_review"
    # op1 (classifier, not the assigned reviewer) tries to kick it back.
    r = client.post(f"/api/tiles/{tile['id']}/request-changes", headers=h(t1), json={"note": "x"})
    assert r.status_code == 403
    conn = connect()
    try:
        row = conn.execute("SELECT status FROM tiles WHERE id=?", (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_review"


# ---------- Admin gate on every admin endpoint ----------

def test_operator_token_rejected_on_all_admin_endpoints(client, operators, tiles):
    t = token(client, "op1", "secret123")
    endpoints = [
        ("GET", "/api/admin/dashboard"),
        ("GET", "/api/admin/tiles"),
        ("GET", "/api/admin/tiles/problems"),
        ("GET", "/api/admin/users"),
    ]
    for method, url in endpoints:
        r = client.request(method, url, headers=h(t))
        assert r.status_code == 403, f"{method} {url} expected 403 got {r.status_code}"

    # Write endpoints (body required)
    assert client.post("/api/admin/tiles/bulk/reset",
                       headers=h(t), json={"ids": [1]}).status_code == 403
    assert client.post("/api/admin/tiles/bulk/re-review",
                       headers=h(t), json={"ids": [1]}).status_code == 403
    assert client.post("/api/admin/tiles/1/reset", headers=h(t)).status_code == 403
    assert client.post("/api/admin/tiles/1/re-review", headers=h(t)).status_code == 403
    assert client.post("/api/admin/users", headers=h(t),
                       json={"username": "x", "password": "y123456",
                             "role": "operator"}).status_code == 403
    assert client.patch("/api/admin/users/1/active",
                        headers=h(t), json={"active": False}).status_code == 403


def test_tile_submit_on_nonexistent_tile_returns_404(client, operators):
    """Unknown tile is 404 (project_for_tile resolves first), never 403/500."""
    t = token(client, "op1", "secret123")
    r = _post_mask(client, t, "/api/tiles/99999/classify")
    assert r.status_code == 404
