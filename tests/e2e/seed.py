"""Seed a fresh DB with admin, operators, and tiles for E2E.
Called from runner.mjs before launching Puppeteer.

Usage: python -m tests.e2e.seed <db_path>
"""
import os
import sys
from datetime import datetime, timezone


def main(db_path: str) -> None:
    os.environ["TILECLASS_DB_PATH"] = db_path  # not used but documents intent
    # Force backend to use this DB
    import backend.config as cfgmod
    import backend.database as dbmod
    import backend.auth as authmod

    original = cfgmod.get_config
    def patched():
        c = original()
        return {**c, "database": {"path": db_path}}
    cfgmod.get_config = patched
    dbmod.get_config = patched
    authmod.get_config = patched
    dbmod._DB_PATH = None

    from backend.database import connect, init_db
    from backend.auth import hash_password
    from backend.mask_utils import empty_mask_png

    init_db()
    conn = connect()
    now = datetime.now(timezone.utc).isoformat()
    try:
        # init_db seeded a default project from config.yaml; pin its id.
        pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1"
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) "
            "VALUES (?,?,?,1,?)",
            ("admin", hash_password("admin123"), "admin", now),
        )
        admin_id = conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
            (pid, admin_id, "admin"),
        )

        for i in range(1, 4):
            conn.execute(
                "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
                "VALUES (?,?,?,1,1,?)",
                (f"op{i}", hash_password("secret123"), "operator", now),
            )
            uid = conn.execute(
                "SELECT id FROM users WHERE username=?", (f"op{i}",)
            ).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
                (pid, uid, "reviewer"),
            )

        empty = empty_mask_png()
        for i in range(10):
            conn.execute(
                """INSERT INTO tiles(project_id, name,
                                     bbox_west, bbox_south, bbox_east, bbox_north,
                                     status, data_png)
                   VALUES (?,?,?,?,?,?,'pending',?)""",
                (pid, f"e2e_{i:03d}", i * 0.1, 0.0, (i + 1) * 0.1, 0.1, empty),
            )
    finally:
        conn.close()
    print("seeded")


if __name__ == "__main__":
    main(sys.argv[1])
