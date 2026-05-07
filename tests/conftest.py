"""Pytest fixtures: isolated SQLite DB per test + FastAPI TestClient."""
import os
import tempfile
from datetime import datetime, timezone
import bcrypt as _bcrypt
import pytest
from fastapi.testclient import TestClient


# Pre-compute fixture password hashes at cost=4 (vs prod cost=12). bcrypt's
# verify path doesn't care about cost, so login flows in tests work the same;
# we save ~2.5s per `operators_10` and ~750ms per `operators` fixture. The
# cost-≥-12 invariant test calls auth.hash_password directly and is unaffected.
_FIXTURE_ADMIN_HASH = _bcrypt.hashpw(b"admin123", _bcrypt.gensalt(rounds=4)).decode()
_FIXTURE_OP_HASH = _bcrypt.hashpw(b"secret123", _bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    """Point backend to a temp SQLite file + temp JWT secret per test."""
    db_file = tmp_path / "test.db"
    cache_file = tmp_path / "mask_overlay_cache.mbtiles"

    import backend.config as config_mod
    import backend.database as dbmod
    import backend.auth as authmod
    import backend.mask_tile_service as mtsmod

    original = config_mod.get_config

    def patched():
        cfg = original()
        return {
            **cfg,
            "database": {"path": str(db_file)},
            # Per-test cache file so invalidation tests don't pollute each other
            # (the service's resolved path is also reset below).
            "mask_overlay": {
                "cache_path": str(cache_file),
                "min_zoom": 8,
                "max_zoom": 18,
            },
        }

    # Patch the references actually used inside each module (from ... import binds early).
    monkeypatch.setattr(config_mod, "get_config", patched, raising=True)
    monkeypatch.setattr(dbmod, "get_config", patched, raising=True)
    monkeypatch.setattr(authmod, "get_config", patched, raising=True)
    monkeypatch.setattr(mtsmod, "get_config", patched, raising=True)
    # admin_service, tile_service import config inside functions, so patching config_mod is enough
    monkeypatch.setattr(dbmod, "_DB_PATH", None, raising=True)
    mtsmod.reset_cache_path()

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


def _wal_checkpoint():
    """Force a WAL→main-DB flush after fixture writes. Reduces (does not fully
    eliminate) a Windows-only flake where the FastAPI request thread saw
    stale state on the very first read after a commit; the residual ~0.5%
    rate is mopped up by a single retry inside the `token()` helper below."""
    from backend.database import connect
    conn = connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _add_to_default_project(conn, user_id: int, role: str) -> None:
    """Make the user a member of the seed default project so project-aware
    endpoints can serve them tiles in tests."""
    row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
        (row["id"], user_id, role),
    )


@pytest.fixture()
def admin_user(app_env):
    # Use transaction() so BEGIN/COMMIT pairs explicitly — bare autocommit
    # writes were occasionally invisible to the FastAPI threadpool on Windows
    # (WAL flush race), causing flaky 401s on the first login.
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
            ("admin", _FIXTURE_ADMIN_HASH, "admin", datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        _add_to_default_project(conn, row["id"], "admin")
    _wal_checkpoint()
    return {"id": row["id"], "username": "admin", "password": "admin123"}


def _make_operators(n: int):
    from backend.database import transaction
    now = datetime.now(timezone.utc).isoformat()
    users = []
    with transaction("IMMEDIATE") as conn:
        for i in range(n):
            u = f"op{i+1}"
            conn.execute(
                "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
                "VALUES (?,?,?,1,1,?)",
                (u, _FIXTURE_OP_HASH, "operator", now),
            )
            row = conn.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
            _add_to_default_project(conn, row["id"], "reviewer")
            users.append({"id": row["id"], "username": u, "password": "secret123"})
    _wal_checkpoint()
    return users


@pytest.fixture()
def operators(app_env):
    return _make_operators(3)


@pytest.fixture()
def operators_10(app_env):
    return _make_operators(10)


def _default_project_id(conn) -> int:
    row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("default project missing — init_db should have seeded it")
    return row["id"]


@pytest.fixture()
def default_project(app_env):
    """Resolve the auto-seeded default project id (created by init_db)."""
    from backend.database import connect
    conn = connect()
    try:
        return _default_project_id(conn)
    finally:
        conn.close()


@pytest.fixture()
def tiles_many(app_env):
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    empty = empty_mask_png()
    conn = connect()
    try:
        pid = _default_project_id(conn)
        for i in range(100):
            conn.execute(
                """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                   status,data_png)
                   VALUES (?,?,?,?,?,?,'pending',?)""",
                (pid, f"tile_{i:03d}", 0.0 + i, 0.0, 0.1 + i, 0.1, empty),
            )
    finally:
        conn.close()


@pytest.fixture()
def tiles(app_env):
    """Insert 10 pending tiles in the default project."""
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    empty = empty_mask_png()
    conn = connect()
    try:
        pid = _default_project_id(conn)
        for i in range(10):
            conn.execute(
                """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                   status,data_png)
                   VALUES (?,?,?,?,?,?,'pending',?)""",
                (pid, f"tile_{i:03d}", 0.0 + i, 0.0, 0.1 + i, 0.1, empty),
            )
    finally:
        conn.close()


def token(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    # Pragmatic retry: a residual ~0.5% flake on Windows shows up as a 401 on
    # the FIRST login right after a fixture inserts the user — even with
    # explicit BEGIN/COMMIT + wal_checkpoint(TRUNCATE) we still see SQLite
    # occasionally not propagate the freshly-committed row to the FastAPI
    # request thread. A single retry consistently passes; we only retry on a
    # plain 401 (real auth bugs would surface in the rest of the test).
    if r.status_code == 401:
        r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]
