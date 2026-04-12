"""Rate limit behaves correctly without relying on reset_rate_limits()."""
import time
import pytest
from tests.conftest import token


def test_6th_login_throttled_without_any_reset(client, admin_user):
    """
    No calls to reset_rate_limits() beyond the conftest start. 5 wrong passwords
    must go through (as 401), 6th must be 429, regardless of username.
    """
    for i in range(5):
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": f"nope{i}"})
        assert r.status_code == 401, f"attempt {i+1} was {r.status_code}"

    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 429


def test_rate_limit_window_expires_after_60s(client, admin_user, monkeypatch):
    """Monkeypatch time.time() to jump 61s → 6th attempt passes again."""
    from backend import auth as authmod

    real_time = time.time
    offset = [0.0]
    monkeypatch.setattr(authmod.time, "time", lambda: real_time() + offset[0])

    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "x"})

    # Advance 61s — stale attempts must be purged
    offset[0] = 61.0
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text


def test_rate_limit_counts_successful_logins_too(client, admin_user):
    """Rate limit is per-IP regardless of outcome — protects against credential
    stuffing where some attempts happen to succeed."""
    from backend import auth as authmod
    authmod.reset_rate_limits()

    for _ in range(5):
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200

    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 429
