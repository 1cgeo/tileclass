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


def test_thumbnail(client, admin_user, tiles):
    t = token(client, "admin", "admin123")
    r = client.get("/api/admin/dashboard", headers=headers(t))
    tiles_list = client.get("/api/admin/tiles", headers=headers(t)).json()
    tid = tiles_list[0]["id"]
    r = client.get(f"/api/admin/tiles/{tid}/thumbnail", headers=headers(t))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
