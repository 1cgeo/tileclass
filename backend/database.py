"""SQLite connection and schema init. No ORM — raw sqlite3 with parameterized queries."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from .config import get_config

_DB_PATH = None


def _db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        p = get_config()["database"]["path"]
        _DB_PATH = Path(p) if Path(p).is_absolute() else Path(__file__).parent / p
    return _DB_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection. Defaults to the configured DB; pass `path` for scripts
    that operate on an arbitrary database file (backup, merge, migration)."""
    conn = sqlite3.connect(path or _db_path(), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(mode: str = "IMMEDIATE", path: Path | None = None):
    conn = connect(path)
    try:
        conn.execute(f"BEGIN {mode}")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('operator','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    can_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    mask_complete_required INTEGER NOT NULL DEFAULT 1,
    primary_mbtiles TEXT NOT NULL,
    secondary_mbtiles TEXT,
    tertiary_mbtiles TEXT,
    ref_mask_primary_mbtiles TEXT,
    ref_mask_secondary_mbtiles TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_classes (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    class_id INTEGER NOT NULL CHECK(class_id BETWEEN 1 AND 254),
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    ordering INTEGER NOT NULL,
    PRIMARY KEY (project_id, class_id)
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('operator','reviewer','admin')),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_pm_user ON project_members(user_id);

CREATE TABLE IF NOT EXISTS tiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
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

CREATE INDEX IF NOT EXISTS idx_tiles_status ON tiles(status);
CREATE INDEX IF NOT EXISTS idx_tiles_assigned ON tiles(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tiles_classified_by ON tiles(classified_by);
CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_by ON tiles(reviewed_by);
CREATE INDEX IF NOT EXISTS idx_tiles_classified_at ON tiles(classified_at);
CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_at ON tiles(reviewed_at);
-- project_id indices are created post-migration; legacy DBs add the column
-- in _migrate() before _rebuild_tiles_with_project_not_null() recreates them.

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    tile_id INTEGER REFERENCES tiles(id),
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_user ON action_log(user_id);
CREATE INDEX IF NOT EXISTS idx_log_tile ON action_log(tile_id);
CREATE INDEX IF NOT EXISTS idx_log_created ON action_log(created_at);
-- Dashboard pairs assign_*→classify/review by (user_id, tile_id, action).
CREATE INDEX IF NOT EXISTS idx_log_user_tile_action ON action_log(user_id, tile_id, action);

CREATE TABLE IF NOT EXISTS rate_limit (
    ip TEXT NOT NULL,
    attempted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_ip ON rate_limit(ip);
CREATE INDEX IF NOT EXISTS idx_rl_ts ON rate_limit(attempted_at);

CREATE TABLE IF NOT EXISTS token_blacklist (
    jti TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tb_exp ON token_blacklist(expires_at);
"""


def _seed_default_project(conn: sqlite3.Connection) -> int | None:
    """Create the seed project from config.yaml if no project exists yet.
    Returns the new project id, or None when projects already exist."""
    has = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
    if has:
        return None
    cfg = get_config()
    dp = cfg.get("default_project") or {}
    name = dp.get("name") or "default"
    description = dp.get("description") or ""
    mask_required = 1 if dp.get("mask_complete_required", True) else 0
    primary = (cfg.get("tileserver") or {}).get("mbtiles_path") or ""
    secondary = (cfg.get("tileserver_secondary") or {}).get("mbtiles_path") or None
    tertiary = (cfg.get("tileserver_tertiary") or {}).get("mbtiles_path") or None
    ref_primary = (cfg.get("dsg") or {}).get("mbtiles_path") or None
    ref_secondary = (cfg.get("mapbiomas") or {}).get("mbtiles_path") or None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO projects(name, description, mask_complete_required,
           primary_mbtiles, secondary_mbtiles, tertiary_mbtiles,
           ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
           active, created_at)
           VALUES (?,?,?,?,?,?,?,?,1,?)""",
        (name, description, mask_required, primary, secondary, tertiary,
         ref_primary, ref_secondary, now),
    )
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for ord_idx, c in enumerate(cfg.get("classes") or []):
        conn.execute(
            """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
               VALUES (?,?,?,?,?)""",
            (pid, c["id"], c["name"], c["color"], ord_idx),
        )
    return pid


def _seed_members(conn: sqlite3.Connection, project_id: int) -> None:
    """Add every active user as a member of the given project, with role
    derived from their global role/can_review flag."""
    users = conn.execute("SELECT id, role, can_review FROM users WHERE active=1").fetchall()
    for u in users:
        if u["role"] == "admin":
            role = "admin"
        elif u["can_review"]:
            role = "reviewer"
        else:
            role = "operator"
        conn.execute(
            "INSERT OR IGNORE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
            (project_id, u["id"], role),
        )


def _rebuild_tiles_with_project_not_null(conn: sqlite3.Connection) -> None:
    """SQLite cannot ALTER COLUMN to NOT NULL. Rebuild via shadow table."""
    conn.execute(
        """CREATE TABLE tiles_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
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
        )"""
    )
    conn.execute(
        """INSERT INTO tiles_new (id, project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
           status, assigned_to, classified_by, reviewed_by, classified_at, reviewed_at,
           data_png, problem_note, version, paused_at, blocked_from)
           SELECT id, project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                  status, assigned_to, classified_by, reviewed_by, classified_at, reviewed_at,
                  data_png, problem_note, version, paused_at, blocked_from
           FROM tiles"""
    )
    conn.execute("DROP TABLE tiles")
    conn.execute("ALTER TABLE tiles_new RENAME TO tiles")
    # Recreate indices that lived on the old table.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_status ON tiles(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_assigned ON tiles(assigned_to)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_classified_by ON tiles(classified_by)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_by ON tiles(reviewed_by)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_classified_at ON tiles(classified_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_at ON tiles(reviewed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_project_status ON tiles(project_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_project_assigned ON tiles(project_id, assigned_to)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tiles_paused "
        "ON tiles(paused_at) WHERE paused_at IS NOT NULL"
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for pre-existing DBs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
    if "version" not in cols:
        conn.execute("ALTER TABLE tiles ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if "paused_at" not in cols:
        conn.execute("ALTER TABLE tiles ADD COLUMN paused_at TEXT")
    if "blocked_from" not in cols:
        # Remembers which status to restore on unblock. Set only when status='blocked'.
        conn.execute("ALTER TABLE tiles ADD COLUMN blocked_from TEXT")
    # Indices on migrated columns must run after the ALTER above (cannot live
    # in SCHEMA because executescript runs before this fn on existing DBs).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tiles_paused "
        "ON tiles(paused_at) WHERE paused_at IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_pause_resume "
        "ON action_log(user_id, tile_id, created_at) WHERE action IN ('pause','resume')"
    )
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "can_review" not in user_cols:
        # Admins get review rights automatically (role check runs first anyway);
        # operators stay at 0 until an admin opts them in.
        conn.execute("ALTER TABLE users ADD COLUMN can_review INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET can_review=1 WHERE role='admin'")

    # Project migration: pre-project DBs have a `tiles` table without project_id.
    # We add the column nullable, seed the default project from config.yaml,
    # backfill, then rebuild the table to enforce NOT NULL.
    if "project_id" not in cols:
        conn.execute("ALTER TABLE tiles ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        pid = _seed_default_project(conn)
        if pid is None:
            row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
            pid = row["id"] if row else None
        if pid is None:
            raise RuntimeError("project migration: no project to backfill into")
        conn.execute("UPDATE tiles SET project_id=? WHERE project_id IS NULL", (pid,))
        _rebuild_tiles_with_project_not_null(conn)
        _seed_members(conn, pid)


def _create_project_id_indices(conn: sqlite3.Connection) -> None:
    """Indices on tiles.project_id can only be created after the column exists.
    Idempotent: creates only when the column is present (true after _migrate)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
    if "project_id" not in cols:
        return
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_project_status ON tiles(project_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_project_assigned ON tiles(project_id, assigned_to)")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Fresh DBs: seed the default project from config.yaml so the app is
        # immediately usable. Idempotent — no-op once any project exists.
        pid = _seed_default_project(conn)
        if pid is not None:
            _seed_members(conn, pid)
        _create_project_id_indices(conn)
    finally:
        conn.close()


def log_action(conn: sqlite3.Connection, user_id: int, tile_id: int | None, action: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (user_id, tile_id, action, detail, datetime.now(timezone.utc).isoformat()),
    )
