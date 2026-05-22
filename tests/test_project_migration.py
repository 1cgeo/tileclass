"""Migration: pre-project DBs gain a default project, all tiles get
project_id, members seed for active users, schema becomes NOT NULL."""
import sqlite3
from datetime import datetime, timezone


def _make_legacy_db(path):
    """Create a DB that matches the pre-project schema (no projects, no
    project_id on tiles, no project_classes/project_members)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('operator','admin')),
            active INTEGER NOT NULL DEFAULT 1,
            can_review INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE tiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            bbox_west REAL NOT NULL,
            bbox_south REAL NOT NULL,
            bbox_east REAL NOT NULL,
            bbox_north REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            assigned_to INTEGER REFERENCES users(id),
            classified_by INTEGER REFERENCES users(id),
            reviewed_by INTEGER REFERENCES users(id),
            classified_at TEXT,
            reviewed_at TEXT,
            data_png BLOB,
            problem_note TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            paused_at TEXT,
            blocked_from TEXT
        );
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            tile_id INTEGER REFERENCES tiles(id),
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE rate_limit (ip TEXT NOT NULL, attempted_at REAL NOT NULL);
        CREATE TABLE token_blacklist (
            jti TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL
        );
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
        "VALUES (?,?,?,1,0,?)", ("alice", "x", "operator", now),
    )
    conn.execute(
        "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
        "VALUES (?,?,?,1,1,?)", ("bob", "x", "operator", now),
    )
    conn.execute(
        "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
        "VALUES (?,?,?,1,1,?)", ("admin", "x", "admin", now),
    )
    # Inactive user — must NOT become a project member.
    conn.execute(
        "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
        "VALUES (?,?,?,0,0,?)", ("ghost", "x", "operator", now),
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO tiles(name,bbox_west,bbox_south,bbox_east,bbox_north,status) "
            "VALUES (?,?,?,?,?, 'pending')",
            (f"tile_{i}", 0.0 + i, 0.0, 0.1 + i, 0.1),
        )
    # action_log rows that FK-reference tiles. A real DB always has these; the
    # NOT NULL rebuild must drop the old `tiles` table while they exist, which
    # fails under foreign_keys=ON unless the migration disables FKs around the
    # swap. Without these rows the rebuild path is never stressed.
    for tid in (1, 2, 3):
        conn.execute(
            "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) "
            "VALUES (?,?,?,?,?)", (3, tid, "assign_classify", None, now),
        )
    conn.close()


def test_migration_creates_default_project_and_backfills(app_env, tmp_path, monkeypatch):
    """Running init_db on a legacy DB seeds projects + backfills tiles."""
    legacy = tmp_path / "legacy.db"
    _make_legacy_db(legacy)

    import backend.database as dbmod
    # Point the module's lazy DB resolver at the legacy file.
    monkeypatch.setattr(dbmod, "_DB_PATH", legacy, raising=True)
    dbmod.init_db()

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    try:
        # 1. Project tables exist.
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"projects", "project_classes", "project_members"} <= tables

        # 2. Exactly one project (default), seeded from config.yaml.
        projects = conn.execute("SELECT * FROM projects").fetchall()
        assert len(projects) == 1
        default = projects[0]
        assert default["name"] == "default"
        assert default["mask_complete_required"] == 1

        # 3. Classes copied from config.yaml.classes.
        classes = conn.execute(
            "SELECT class_id, name, color, ordering FROM project_classes "
            "WHERE project_id=? ORDER BY ordering",
            (default["id"],),
        ).fetchall()
        assert len(classes) >= 1
        # First class from the seed config has id=1.
        assert classes[0]["class_id"] == 1

        # 4. All existing tiles got project_id = default.
        tiles = conn.execute("SELECT id, project_id FROM tiles").fetchall()
        assert len(tiles) == 5
        assert all(t["project_id"] == default["id"] for t in tiles)

        # 5. Schema enforces NOT NULL on project_id now (rebuilt).
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
        assert info["project_id"]["notnull"] == 1

        # 6. Active users became members; inactive 'ghost' did not.
        members = conn.execute(
            "SELECT u.username, m.role FROM project_members m "
            "JOIN users u ON u.id=m.user_id WHERE m.project_id=?",
            (default["id"],),
        ).fetchall()
        by_name = {m["username"]: m["role"] for m in members}
        assert by_name == {"alice": "operator", "bob": "reviewer", "admin": "admin"}
    finally:
        conn.close()


def test_migration_is_idempotent(app_env, tmp_path, monkeypatch):
    """Running init_db twice on a legacy DB must not duplicate projects/members."""
    legacy = tmp_path / "legacy.db"
    _make_legacy_db(legacy)

    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "_DB_PATH", legacy, raising=True)
    dbmod.init_db()
    dbmod.init_db()

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM project_members").fetchone()["c"] == 3
    finally:
        conn.close()


def test_migration_preserves_action_log_referencing_tiles(app_env, tmp_path, monkeypatch):
    """The NOT NULL rebuild drops/recreates `tiles` while action_log FK-references
    it. Migration must complete and leave those rows (and their tile_id) intact."""
    legacy = tmp_path / "legacy.db"
    _make_legacy_db(legacy)

    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "_DB_PATH", legacy, raising=True)
    dbmod.init_db()

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    try:
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
        assert info["project_id"]["notnull"] == 1
        log = conn.execute(
            "SELECT tile_id FROM action_log ORDER BY tile_id"
        ).fetchall()
        assert [r["tile_id"] for r in log] == [1, 2, 3]
        # No dangling FK and no leftover shadow table.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='tiles_new'"
        ).fetchone() is None
    finally:
        conn.close()


def test_migration_resumes_after_partial_rebuild(app_env, tmp_path, monkeypatch):
    """A run that aborted after adding the nullable project_id column but before
    enforcing NOT NULL (orphan tiles_new, unseeded members) must be completed by
    a subsequent init_db, not skipped because the column already exists."""
    legacy = tmp_path / "legacy.db"
    _make_legacy_db(legacy)

    # Simulate the half-migrated state of a crash mid-rebuild.
    conn = sqlite3.connect(legacy, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            description TEXT, mask_complete_required INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO projects(name) VALUES ('default');
        """
    )
    conn.execute("ALTER TABLE tiles ADD COLUMN project_id INTEGER REFERENCES projects(id)")
    conn.execute("UPDATE tiles SET project_id=1")
    conn.execute("CREATE TABLE tiles_new (id INTEGER PRIMARY KEY)")  # orphan from abort
    conn.close()

    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "_DB_PATH", legacy, raising=True)
    dbmod.init_db()

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    try:
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
        assert info["project_id"]["notnull"] == 1  # rebuild finished
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='tiles_new'"
        ).fetchone() is None  # orphan cleaned up
        assert conn.execute("SELECT COUNT(*) c FROM project_members").fetchone()["c"] == 3
        assert all(t["project_id"] == 1 for t in conn.execute("SELECT project_id FROM tiles"))
    finally:
        conn.close()


def test_fresh_db_seeds_default_project(app_env):
    """A fresh DB initialised by the fixture must already have the default project."""
    from backend.database import connect
    conn = connect()
    try:
        rows = conn.execute("SELECT name FROM projects").fetchall()
        assert [r["name"] for r in rows] == ["default"]
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
        assert info["project_id"]["notnull"] == 1
    finally:
        conn.close()
