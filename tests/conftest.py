"""Pytest fixtures: isolated SQLite DB per test + FastAPI TestClient."""
import os
import tempfile
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    """Point backend to a temp SQLite file + temp JWT secret per test."""
    db_file = tmp_path / "test.db"

    import backend.config as config_mod
    import backend.database as dbmod
    import backend.auth as authmod

    original = config_mod.get_config

    def patched():
        cfg = original()
        return {**cfg, "database": {"path": str(db_file)}}

    # Patch the references actually used inside each module (from ... import binds early).
    monkeypatch.setattr(config_mod, "get_config", patched, raising=True)
    monkeypatch.setattr(dbmod, "get_config", patched, raising=True)
    monkeypatch.setattr(authmod, "get_config", patched, raising=True)
    # admin_service, tile_service import config inside functions, so patching config_mod is enough
    monkeypatch.setattr(dbmod, "_DB_PATH", None, raising=True)

    # Reset in-memory rate-limit state between tests
    authmod.reset_rate_limits()

    # Tests use the placeholder secret from config.yaml; opt-in explicitly.
    monkeypatch.setenv("TILECLASS_ALLOW_DEFAULT_SECRET", "1")

    dbmod.init_db()
    yield db_file


@pytest.fixture()
def client(app_env):
    from backend.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_user(app_env):
    from backend.database import connect
    from backend.auth import hash_password
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
            ("admin", hash_password("admin123"), "admin", datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    finally:
        conn.close()
    return {"id": row["id"], "username": "admin", "password": "admin123"}


def _make_operators(n: int):
    from backend.database import connect
    from backend.auth import hash_password
    now = datetime.now(timezone.utc).isoformat()
    users = []
    conn = connect()
    try:
        for i in range(n):
            u = f"op{i+1}"
            conn.execute(
                "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
                "VALUES (?,?,?,1,1,?)",
                (u, hash_password("secret123"), "operator", now),
            )
            row = conn.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
            users.append({"id": row["id"], "username": u, "password": "secret123"})
    finally:
        conn.close()
    return users


@pytest.fixture()
def operators(app_env):
    return _make_operators(3)


@pytest.fixture()
def operators_10(app_env):
    return _make_operators(10)


@pytest.fixture()
def tiles_many(app_env):
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    empty = empty_mask_png()
    conn = connect()
    try:
        for i in range(100):
            conn.execute(
                """INSERT INTO tiles(name,bbox_west,bbox_south,bbox_east,bbox_north,
                   status,data_png)
                   VALUES (?,?,?,?,?,'pending',?)""",
                (f"tile_{i:03d}", 0.0 + i, 0.0, 0.1 + i, 0.1, empty),
            )
    finally:
        conn.close()


@pytest.fixture()
def tiles(app_env):
    """Insert 10 pending tiles."""
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    empty = empty_mask_png()
    conn = connect()
    try:
        for i in range(10):
            conn.execute(
                """INSERT INTO tiles(name,bbox_west,bbox_south,bbox_east,bbox_north,
                   status,data_png)
                   VALUES (?,?,?,?,?,'pending',?)""",
                (f"tile_{i:03d}", 0.0 + i, 0.0, 0.1 + i, 0.1, empty),
            )
    finally:
        conn.close()


def token(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]
