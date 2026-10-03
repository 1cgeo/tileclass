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
    # WAL allows a single writer; without a busy timeout a concurrent
    # `BEGIN IMMEDIATE` (e.g. several operators hitting /api/tiles/next at once)
    # would fail fast with SQLITE_BUSY instead of briefly waiting for the lock.
    conn.execute("PRAGMA busy_timeout=5000")
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
    -- 'raster' (pixel-mask flow) or 'classification' (single class_id per
    -- tile). Immutable after creation: changing kind would orphan every tile
    -- body. 'vector'/'detection' are retired kinds kept in the CHECK only so
    -- legacy DBs stay valid; project_service.PROJECT_KINDS gates creation.
    kind TEXT NOT NULL DEFAULT 'raster'
        CHECK (kind IN ('raster','vector','classification','detection')),
    -- Legacy (retired vector/detection kinds). Unused; kept so the
    -- migration chain and merge_db still copy old rows verbatim.
    topology_required INTEGER NOT NULL DEFAULT 0,
    box_required INTEGER NOT NULL DEFAULT 0,
    -- Tile geometry. tile_meters = tile_px * meters_per_pixel; mask body
    -- size = tile_px². Locked once any tile exists (mask bytes assume the
    -- original shape).
    tile_px INTEGER NOT NULL DEFAULT 256 CHECK (tile_px > 0),
    meters_per_pixel REAL NOT NULL DEFAULT 2.5 CHECK (meters_per_pixel > 0),
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

-- Legacy: attribute schema of the retired 'vector' kind. Unused by the app;
-- kept so legacy DBs and merge_db keep working.
CREATE TABLE IF NOT EXISTS project_attributes (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('text','number','enum','boolean')),
    required INTEGER NOT NULL DEFAULT 0,
    options_json TEXT,
    ordering INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, key)
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
    blocked_from TEXT,
    -- JSON: {"<class_id>": <pixel_count>, ...}. Populated on submit; null
    -- when the tile has never been classified. Used by the dashboard's
    -- class-distribution panel without re-decoding masks.
    class_counts TEXT,
    last_heartbeat_at TEXT,
    -- Legacy body of the retired vector/detection kinds. Unused by the app.
    data_geojson TEXT,
    feature_count INTEGER,
    -- Classification tiles only: the single class id assigned to the tile.
    -- Null on raster tiles and on never-classified rows.
    data_class_id INTEGER
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

-- Background export jobs: large datasets are zipped off the request thread so
-- the admin can poll and download when ready instead of holding a long HTTP
-- request open. The zip lives on disk (file_path) until downloaded.
CREATE TABLE IF NOT EXISTS export_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status_param TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','error')),
    file_path TEXT,
    filename TEXT,
    tile_count INTEGER,
    error TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_project ON export_jobs(project_id);
"""


# Palette baked into the legacy migration only. A fresh install starts with no
# project — the admin creates and configures everything (classes, layers,
# geometry) through the admin UI. This constant exists solely so a pre-projects
# DB being migrated has a home for its orphan tiles whose masks may reference
# these historical TileClass ids; names/colors are editable afterwards.
_MIGRATION_CLASSES = (
    (1, "Massa d'água", "#377eb8"),
    (2, "Área edificada", "#e41a1c"),
    (3, "Floresta", "#4daf4a"),
    (4, "Campo", "#ffff33"),
    (5, "Cultivo", "#984ea3"),
    (6, "Terreno exposto", "#ff7f00"),
)


def _migration_seed_project(conn: sqlite3.Connection) -> int | None:
    """Create the project that orphan tiles are backfilled into when migrating
    a pre-projects DB. Returns its id, or None when a project already exists
    (then the caller backfills into the existing one). Layers start empty —
    the admin points them at mbtiles/URLs via the UI. NOT read from config:
    domain config lives in the DB, not config.yaml."""
    has = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
    if has:
        return None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO projects(name, description, mask_complete_required,
           primary_mbtiles, active, created_at)
           VALUES (?,?,?,?,1,?)""",
        ("default", "", 1, "", now),
    )
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for ord_idx, (cid, name, color) in enumerate(_MIGRATION_CLASSES):
        conn.execute(
            """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
               VALUES (?,?,?,?,?)""",
            (pid, cid, name, color, ord_idx),
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


def _rebuild_projects_widen_kind_check(conn: sqlite3.Connection) -> None:
    """Widen the projects.kind CHECK to include 'detection'. SQLite can't ALTER
    a CHECK, so rebuild via shadow table. Idempotent: skips when the current
    table SQL already allows 'detection'. Other tables FK-reference projects
    (project_classes/attributes/members/tiles), so the swap runs with
    foreign_keys=OFF in one transaction, then re-checks FK integrity."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if not row or "'detection'" in row[0]:
        return
    conn.execute("DROP TABLE IF EXISTS projects_rebuild")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE projects_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                kind TEXT NOT NULL DEFAULT 'raster'
                    CHECK (kind IN ('raster','vector','classification','detection')),
                topology_required INTEGER NOT NULL DEFAULT 0,
                box_required INTEGER NOT NULL DEFAULT 0,
                tile_px INTEGER NOT NULL DEFAULT 256 CHECK (tile_px > 0),
                meters_per_pixel REAL NOT NULL DEFAULT 2.5 CHECK (meters_per_pixel > 0),
                mask_complete_required INTEGER NOT NULL DEFAULT 1,
                primary_mbtiles TEXT NOT NULL,
                secondary_mbtiles TEXT,
                tertiary_mbtiles TEXT,
                ref_mask_primary_mbtiles TEXT,
                ref_mask_secondary_mbtiles TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO projects_rebuild (id, name, description, kind,
               topology_required, box_required, tile_px, meters_per_pixel,
               mask_complete_required, primary_mbtiles, secondary_mbtiles,
               tertiary_mbtiles, ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
               active, created_by, created_at)
               SELECT id, name, description, kind,
               topology_required, box_required, tile_px, meters_per_pixel,
               mask_complete_required, primary_mbtiles, secondary_mbtiles,
               tertiary_mbtiles, ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
               active, created_by, created_at FROM projects"""
        )
        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_rebuild RENAME TO projects")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys=ON")
    if violations:
        raise RuntimeError(
            f"projects kind-check rebuild: foreign-key violations: {violations}"
        )


def _rebuild_tiles_with_project_not_null(conn: sqlite3.Connection) -> None:
    """SQLite cannot ALTER COLUMN to NOT NULL. Rebuild via shadow table.

    Other tables (action_log) carry FKs into tiles, so dropping the old table
    with foreign_keys=ON makes SQLite refuse to delete the referenced rows.
    Follow SQLite's canonical schema-change recipe: toggle foreign_keys OFF
    (a no-op inside a transaction, so it must happen outside), do the swap in
    one transaction for atomicity, then re-check FK integrity and re-enable.
    `tiles_new` is dropped first so a prior aborted run can be retried.
    """
    conn.execute("DROP TABLE IF EXISTS tiles_new")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _rebuild_tiles_swap(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys=ON")
    if violations:
        raise RuntimeError(
            f"project migration: foreign-key violations after rebuild: {violations}"
        )


def _rebuild_tiles_swap(conn: sqlite3.Connection) -> None:
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
            blocked_from TEXT,
            class_counts TEXT,
            last_heartbeat_at TEXT,
            data_geojson TEXT,
            feature_count INTEGER,
            data_class_id INTEGER
        )"""
    )
    conn.execute(
        """INSERT INTO tiles_new (id, project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
           status, assigned_to, classified_by, reviewed_by, classified_at, reviewed_at,
           data_png, problem_note, version, paused_at, blocked_from,
           class_counts, last_heartbeat_at, data_geojson, feature_count, data_class_id)
           SELECT id, project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                  status, assigned_to, classified_by, reviewed_by, classified_at, reviewed_at,
                  data_png, problem_note, version, paused_at, blocked_from,
                  class_counts, last_heartbeat_at, data_geojson, feature_count, data_class_id
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
    if "class_counts" not in cols:
        # JSON pixel-count cache populated on submit; legacy tiles stay NULL
        # until `recompute_class_counts.py` is run.
        conn.execute("ALTER TABLE tiles ADD COLUMN class_counts TEXT")
    if "last_heartbeat_at" not in cols:
        # ISO8601 timestamp the editor pings while the tile is open. Stale
        # heartbeat → auto-pause sweep takes over and frees the slot.
        conn.execute("ALTER TABLE tiles ADD COLUMN last_heartbeat_at TEXT")
    if "data_geojson" not in cols:
        # Vector tile body. Null on legacy raster tiles.
        conn.execute("ALTER TABLE tiles ADD COLUMN data_geojson TEXT")
    if "feature_count" not in cols:
        conn.execute("ALTER TABLE tiles ADD COLUMN feature_count INTEGER")
    if "data_class_id" not in cols:
        # Classification tiles: single class id per tile.
        conn.execute("ALTER TABLE tiles ADD COLUMN data_class_id INTEGER")
    # projects.kind / topology_required for the vector flow. Defaults
    # preserve existing rows as raster.
    proj_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "kind" not in proj_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'raster' "
            "CHECK (kind IN ('raster','vector'))"
        )
    if "topology_required" not in proj_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN topology_required INTEGER NOT NULL DEFAULT 0"
        )
    if "tile_px" not in proj_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN tile_px INTEGER NOT NULL DEFAULT 256"
        )
    if "meters_per_pixel" not in proj_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN meters_per_pixel REAL NOT NULL DEFAULT 2.5"
        )
    if "box_required" not in proj_cols:
        # Detection projects: when 1, submit requires ≥1 box.
        conn.execute(
            "ALTER TABLE projects ADD COLUMN box_required INTEGER NOT NULL DEFAULT 0"
        )
    # The projects.kind CHECK predates the 'detection' kind on older DBs.
    # SQLite can't ALTER a CHECK, so rebuild the table to widen it (idempotent:
    # no-op once the constraint already lists 'detection'). Runs after the
    # box_required ADD above so the rebuilt table copies that column.
    _rebuild_projects_widen_kind_check(conn)
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
    # We add the column nullable, create a project to home the orphan tiles,
    # backfill, then rebuild the table to enforce NOT NULL. Gated on project_id
    # being NOT NULL (not merely present) so a run that aborted mid-rebuild —
    # column added but the NOT NULL swap unfinished — resumes instead of being
    # skipped and left half-migrated. Fresh DBs never enter here (the SCHEMA
    # already declares project_id NOT NULL), so they stay empty.
    pid_col = next((r for r in conn.execute("PRAGMA table_info(tiles)").fetchall()
                    if r[1] == "project_id"), None)
    project_id_enforced = pid_col is not None and pid_col[3] == 1  # notnull flag
    if not project_id_enforced:
        if pid_col is None:
            conn.execute("ALTER TABLE tiles ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        pid = _migration_seed_project(conn)
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
        # A fresh DB stays empty: no project, no classes. The admin creates and
        # configures the first project through the admin UI. Only the legacy
        # migration above seeds a project (to home pre-existing orphan tiles).
        _create_project_id_indices(conn)
    finally:
        conn.close()


def now_iso() -> str:
    """UTC timestamp in ISO 8601 — single source for `*_at` columns and
    action_log.created_at across the codebase."""
    return datetime.now(timezone.utc).isoformat()


def log_action(conn: sqlite3.Connection, user_id: int, tile_id: int | None, action: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (user_id, tile_id, action, detail, now_iso()),
    )
