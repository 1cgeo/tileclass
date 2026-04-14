"""Invariants of auth module: bcrypt cost, JWT lifetimes, token tampering."""
import time
import jwt as pyjwt
import pytest
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def test_bcrypt_cost_at_least_12(app_env):
    """CLAUDE.md mandates bcrypt cost >= 12. Hash format: $2b$<cost>$..."""
    from backend.auth import hash_password
    hashed = hash_password("anything")
    parts = hashed.split("$")
    assert parts[0] == "" and parts[1].startswith("2")  # $2a$ or $2b$
    cost = int(parts[2])
    assert cost >= 12, f"bcrypt cost {cost} below required 12"


def test_access_token_exp_is_configured_8h(client, admin_user):
    t = token(client, "admin", "admin123")
    payload = pyjwt.decode(t, options={"verify_signature": False})
    delta = payload["exp"] - payload["iat"]
    assert abs(delta - 8 * 3600) <= 2, f"access token TTL {delta}s != 8h"


def test_refresh_token_exp_is_configured_24h(client, admin_user):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    refresh = r.json()["refresh_token"]
    payload = pyjwt.decode(refresh, options={"verify_signature": False})
    delta = payload["exp"] - payload["iat"]
    assert abs(delta - 24 * 3600) <= 2, f"refresh token TTL {delta}s != 24h"


def test_refresh_token_typ_is_refresh_not_access(client, admin_user):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    access = pyjwt.decode(r.json()["access_token"], options={"verify_signature": False})
    refresh = pyjwt.decode(r.json()["refresh_token"], options={"verify_signature": False})
    assert access["typ"] == "access"
    assert refresh["typ"] == "refresh"


def test_token_with_invalid_signature_returns_401(client, admin_user):
    """Tamper last char of signature → must be rejected."""
    t = token(client, "admin", "admin123")
    parts = t.split(".")
    bad_sig = parts[2][:-2] + ("aa" if parts[2][-1] != "a" else "bb")
    tampered = f"{parts[0]}.{parts[1]}.{bad_sig}"
    r = client.get("/api/auth/me", headers=h(tampered))
    assert r.status_code == 401


def test_forged_admin_token_with_wrong_secret_rejected(client, admin_user):
    """Attacker forges role=admin with wrong secret → backend rejects signature."""
    payload = {"sub": "999", "username": "hacker", "role": "admin",
               "typ": "access", "iat": int(time.time()),
               "exp": int(time.time()) + 3600}
    forged = pyjwt.encode(payload, "wrong-secret-" + "x" * 32, algorithm="HS256")
    r = client.get("/api/admin/dashboard", headers=h(forged))
    assert r.status_code == 401


def test_access_token_used_as_refresh_rejected(client, admin_user):
    """typ=access must not be accepted by /refresh."""
    t = token(client, "admin", "admin123")
    r = client.post("/api/auth/refresh", json={"refresh_token": t})
    assert r.status_code == 401


def test_refresh_token_used_as_access_rejected(client, admin_user):
    """typ=refresh must not be accepted as Bearer on protected routes."""
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    refresh = r.json()["refresh_token"]
    res = client.get("/api/auth/me", headers=h(refresh))
    assert res.status_code == 401


def test_deactivated_user_cannot_use_valid_token(client, admin_user, operators):
    """Mid-session deactivation: the still-valid access token must stop working."""
    op_tok = token(client, "op1", "secret123")
    assert client.get("/api/auth/me", headers=h(op_tok)).status_code == 200

    adm = token(client, "admin", "admin123")
    client.patch(f"/api/admin/users/{operators[0]['id']}/active",
                 headers=h(adm), json={"active": False})

    # Same token — deactivation must be enforced on each request.
    r = client.get("/api/auth/me", headers=h(op_tok))
    assert r.status_code == 401


def test_operator_cannot_escalate_via_malformed_role_claim(client, operators):
    """Token with typ=access but signed correctly and role='admin' from real user.
    We can't forge that without the secret — but verify the role check uses DB, not claim.
    (If backend only trusts JWT role, a stolen secret would allow escalation; but we
    additionally read role from DB in get_current_user — this test locks that in.)"""
    from backend import auth as authmod
    secret = authmod.get_config()["auth"]["jwt_secret"]
    # Forge an access token claiming admin role for op1 (real user id, role=operator in DB)
    payload = {
        "sub": str(operators[0]["id"]),
        "username": "op1",
        "role": "admin",  # lie
        "typ": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    forged = pyjwt.encode(payload, secret, algorithm="HS256")
    # DB role is operator → admin endpoint must 403
    r = client.get("/api/admin/dashboard", headers=h(forged))
    assert r.status_code == 403, (
        "Backend trusted JWT role claim instead of DB role — privilege escalation risk"
    )
