"""SQLite connection and schema init. No ORM — raw sqlite3 with parameterized queries."""
import sqlite3
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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(mode: str = "IMMEDIATE"):
    conn = connect()
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

CREATE TABLE IF NOT EXISTS tiles (
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
    version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tiles_status ON tiles(status);
CREATE INDEX IF NOT EXISTS idx_tiles_assigned ON tiles(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tiles_classified_by ON tiles(classified_by);
CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_by ON tiles(reviewed_by);
CREATE INDEX IF NOT EXISTS idx_tiles_classified_at ON tiles(classified_at);
CREATE INDEX IF NOT EXISTS idx_tiles_reviewed_at ON tiles(reviewed_at);

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


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for pre-existing DBs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tiles)").fetchall()}
    if "version" not in cols:
        conn.execute("ALTER TABLE tiles ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "can_review" not in user_cols:
        # Admins get review rights automatically (role check runs first anyway);
        # operators stay at 0 until an admin opts them in.
        conn.execute("ALTER TABLE users ADD COLUMN can_review INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET can_review=1 WHERE role='admin'")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def log_action(conn: sqlite3.Connection, user_id: int, tile_id: int | None, action: str, detail: str | None = None) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (user_id, tile_id, action, detail, datetime.now(timezone.utc).isoformat()),
    )
