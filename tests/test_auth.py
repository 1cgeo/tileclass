import time
from tests.conftest import token


def test_login_ok(client, admin_user):
    """200 + both tokens decodable with correct typ/sub/role/exp claims."""
    from backend import auth as authmod
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    data = r.json()
    assert data["access_token"] and data["refresh_token"]
    assert data["access_token"] != data["refresh_token"]

    access = authmod.decode_token(data["access_token"])
    assert access["typ"] == "access"
    assert str(access["sub"]) == str(admin_user["id"])
    assert access["username"] == "admin"
    assert access["role"] == "admin"
    assert access["exp"] > time.time()

    refresh = authmod.decode_token(data["refresh_token"])
    assert refresh["typ"] == "refresh"
    assert str(refresh["sub"]) == str(admin_user["id"])
    assert refresh["exp"] > access["exp"]  # refresh outlives access


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


def test_logout_revokes_token(client, admin_user):
    """After logout the same access token is rejected (jti blacklisted) and the
    revocation is persisted in token_blacklist."""
    from backend import auth as authmod
    from backend.database import connect
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    access = r.json()["access_token"]
    H = {"Authorization": f"Bearer {access}"}
    assert client.get("/api/auth/me", headers=H).status_code == 200  # works before
    assert client.post("/api/auth/logout", headers=H).status_code == 200
    # Same token now refused for any authenticated endpoint.
    after = client.get("/api/auth/me", headers=H)
    assert after.status_code == 401
    # Revocation persisted by jti for the right user.
    jti = authmod.decode_token(access)["jti"]
    conn = connect()
    try:
        row = conn.execute("SELECT user_id FROM token_blacklist WHERE jti=?", (jti,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["user_id"] == admin_user["id"]


def test_logout_does_not_affect_other_sessions(client, admin_user):
    """Revoking one token must not revoke a second, independently-issued token
    (distinct jti) for the same user."""
    a = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
    b = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
    assert a != b
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {a}"})
    # Session A revoked, session B still valid.
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {a}"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {b}"}).status_code == 200


def test_refresh(client, admin_user):
    """New access_token from /refresh must actually authenticate against /me."""
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    refresh = r.json()["refresh_token"]
    r2 = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 200
    new_access = r2.json()["access_token"]
    assert new_access

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"
