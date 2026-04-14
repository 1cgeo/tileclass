import numpy as np
from tests.conftest import token


def headers(t): return {"Authorization": f"Bearer {t}"}


def test_dashboard(client, admin_user, tiles):
    t = token(client, "admin", "admin123")
    r = client.get("/api/admin/dashboard", headers=headers(t))
    assert r.status_code == 200
    d = r.json()
    assert d["total_tiles"] == 10
    assert d["totals_by_status"].get("pending") == 10


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
