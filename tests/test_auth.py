from tests.conftest import token


def test_login_ok(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data and "refresh_token" in data


def test_login_wrong_password(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_token(client, admin_user):
    assert client.get("/api/auth/me").status_code == 401
    t = token(client, "admin", "admin123")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_admin_endpoints_forbid_operator(client, operators):
    t = token(client, "op1", "secret123")
    r = client.get("/api/admin/dashboard", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403


def test_refresh(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    refresh = r.json()["refresh_token"]
    r2 = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 200
    assert "access_token" in r2.json()
