"""Admin-only hard delete for tiles already reported as `problem`.

Two-step invariant: an operator reports, an admin deletes. Status-gating
prevents an admin from wiping a tile that's actively in the queue."""
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _report(client, tok, tile_id, note="broken tile"):
    return client.post(f"/api/tiles/{tile_id}/report-problem",
                       headers=h(tok), json={"note": note})


def test_delete_tile_removes_row_and_logs_and_returns_200(
    client, admin_user, operators, tiles
):
    from backend.database import connect
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    r = _report(client, op1, tile["id"], "saindo da imagem")
    assert r.status_code == 200

    adm = token(client, "admin", "admin123")
    # TestClient.delete doesn't take `json=`; use request() to send a body.
    r = client.request("DELETE", f"/api/admin/tiles/{tile['id']}",
                       headers=h(adm), json={"reason": "fora de cobertura"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": tile["id"], "deleted": True}

    conn = connect()
    try:
        row = conn.execute("SELECT id FROM tiles WHERE id=?", (tile["id"],)).fetchone()
        logs = conn.execute(
            "SELECT action, tile_id FROM action_log WHERE tile_id=?",
            (tile["id"],),
        ).fetchall()
        audit = conn.execute(
            "SELECT user_id, action, tile_id, detail FROM action_log "
            "WHERE action='delete_tile'",
        ).fetchall()
    finally:
        conn.close()
    assert row is None, "tile row still present after DELETE"
    assert logs == [], "action_log for deleted tile must be cleaned up"
    assert len(audit) == 1
    assert audit[0]["user_id"] == admin_user["id"]
    assert audit[0]["tile_id"] is None
    # detail encodes the deleted tile's id/name/reason for audit.
    assert f'"tile_id": {tile["id"]}' in audit[0]["detail"]
    assert "fora de cobertura" in audit[0]["detail"]


def test_delete_tile_rejects_non_problem_status(
    client, admin_user, operators, tiles
):
    """Gate: a tile must be flagged as problem first. Otherwise an admin
    could accidentally wipe a pending/in_progress/classified tile."""
    adm = token(client, "admin", "admin123")
    pending_id = client.get("/api/admin/tiles?status=pending",
                            headers=h(adm)).json()[0]["id"]
    r = client.delete(f"/api/admin/tiles/{pending_id}", headers=h(adm))
    assert r.status_code == 409

    # Same for in_progress.
    op1 = token(client, "op1", "secret123")
    in_progress = client.get("/api/tiles/next", headers=h(op1)).json()
    r = client.delete(f"/api/admin/tiles/{in_progress['id']}", headers=h(adm))
    assert r.status_code == 409

    # Same for classified.
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    client.post(f"/api/tiles/{in_progress['id']}/classify",
                headers={**h(op1), "Content-Type": "application/octet-stream"},
                content=raw)
    r = client.delete(f"/api/admin/tiles/{in_progress['id']}", headers=h(adm))
    assert r.status_code == 409


def test_delete_tile_404_for_missing_id(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    r = client.delete("/api/admin/tiles/999999", headers=h(adm))
    assert r.status_code == 404


def test_delete_tile_requires_admin(client, admin_user, operators, tiles):
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    _report(client, op1, tile["id"], "bad")

    # Operator cannot delete even their own problem report.
    r = client.delete(f"/api/admin/tiles/{tile['id']}", headers=h(op1))
    assert r.status_code == 403


def test_delete_tile_appears_in_problems_list_then_disappears(
    client, admin_user, operators, tiles
):
    """The problem list is the UI surface that owns this action — it must
    show the tile before delete and not after."""
    op1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(op1)).json()
    _report(client, op1, tile["id"], "broken")

    adm = token(client, "admin", "admin123")
    before = client.get("/api/admin/tiles/problems", headers=h(adm)).json()
    assert any(p["id"] == tile["id"] for p in before)

    client.delete(f"/api/admin/tiles/{tile['id']}", headers=h(adm))

    after = client.get("/api/admin/tiles/problems", headers=h(adm)).json()
    assert not any(p["id"] == tile["id"] for p in after)
