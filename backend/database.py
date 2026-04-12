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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    bbox_west REAL NOT NULL,
    bbox_south REAL NOT NULL,
    bbox_east REAL NOT NULL,
    bbox_north REAL NOT NULL,
    zoom INTEGER NOT NULL,
    tile_x INTEGER NOT NULL,
    tile_y INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to INTEGER REFERENCES users(id),
    classified_by INTEGER REFERENCES users(id),
    reviewed_by INTEGER REFERENCES users(id),
    classified_at TEXT,
    reviewed_at TEXT,
    data_png BLOB,
    problem_note TEXT,
    context_tiles TEXT
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
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def log_action(conn: sqlite3.Connection, user_id: int, tile_id: int | None, action: str, detail: str | None = None) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (user_id, tile_id, action, detail, datetime.now(timezone.utc).isoformat()),
    )
