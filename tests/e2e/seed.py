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
        # init_db no longer seeds a project (a fresh app starts empty). Create
        # the project the E2E flow classifies against, with the standard classes.
        conn.execute(
            "INSERT INTO projects(id, name, description, kind, tile_px, meters_per_pixel, "
            "mask_complete_required, primary_mbtiles, active, created_at) "
            "VALUES (1,'default','','raster',256,2.5,1,'',1,?)",
            (now,),
        )
        pid = 1
        for ordering, (cid, cname, color) in enumerate([
            (1, "Massa d'água", "#377eb8"), (2, "Área edificada", "#e41a1c"),
            (3, "Floresta", "#4daf4a"), (4, "Campo", "#ffff33"),
            (5, "Cultivo", "#984ea3"), (6, "Terreno exposto", "#ff7f00"),
        ]):
            conn.execute(
                "INSERT INTO project_classes(project_id, class_id, name, color, ordering) "
                "VALUES (1,?,?,?,?)",
                (cid, cname, color, ordering),
            )

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

        # Classification project (id=2). Its operators are members of this
        # project only, so login lands straight on it (no picker). Class ids
        # are deliberately not 1..N so digit shortcuts must map by position.
        conn.execute(
            "INSERT INTO projects(id, name, description, kind, tile_px, meters_per_pixel, "
            "mask_complete_required, primary_mbtiles, active, created_at) "
            "VALUES (2,'classif','','classification',256,2.5,0,'',1,?)",
            (now,),
        )
        for ordering, (cid, cname, color) in enumerate([
            (10, "Urbano", "#e41a1c"), (20, "Água", "#377eb8"), (30, "Solo", "#ff7f00"),
        ]):
            conn.execute(
                "INSERT INTO project_classes(project_id, class_id, name, color, ordering) "
                "VALUES (2,?,?,?,?)",
                (cid, cname, color, ordering),
            )
        cls_ids = {}
        for uname in ("cls1", "cls_paused"):
            conn.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at) "
                "VALUES (?,?,'operator',1,?)",
                (uname, hash_password("secret123"), now),
            )
            cls_ids[uname] = conn.execute(
                "SELECT id FROM users WHERE username=?", (uname,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO project_members(project_id, user_id, role) VALUES (2,?,'operator')",
                (cls_ids[uname],),
            )
        for i in range(5):
            conn.execute(
                """INSERT INTO tiles(project_id, name,
                                     bbox_west, bbox_south, bbox_east, bbox_north, status)
                   VALUES (2,?,?,?,?,?,'pending')""",
                (f"cls_{i:03d}", i * 0.1, 1.0, (i + 1) * 0.1, 1.1),
            )
        # A tile auto-paused by the heartbeat sweep while cls_paused was away:
        # login must offer "Continuar" and resuming must open the editor.
        conn.execute(
            """INSERT INTO tiles(project_id, name,
                                 bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, assigned_to, paused_at)
               VALUES (2,'cls_paused_tile',0,2.0,0.1,2.1,'in_progress',?,?)""",
            (cls_ids["cls_paused"], now),
        )
        # Already-classified tile (class 30 = "Solo") for the admin viewer.
        conn.execute(
            """INSERT INTO tiles(project_id, name,
                                 bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, data_class_id, classified_by, classified_at)
               VALUES (2,'cls_done',0.5,2.0,0.6,2.1,'classified',30,?,?)""",
            (cls_ids["cls1"], now),
        )

        # Raster: a paused tile holding a partial mask (first 1000 px = class
        # 2), for the resume-with-failed-mask-fetch scenario.
        from backend.mask_utils import encode_mask
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) "
            "VALUES ('op_paused',?,'operator',1,?)",
            (hash_password("secret123"), now),
        )
        op_paused = conn.execute(
            "SELECT id FROM users WHERE username='op_paused'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO project_members(project_id, user_id, role) VALUES (1,?,'operator')",
            (op_paused,),
        )
        partial = bytes([2]) * 1000 + bytes([255]) * (65536 - 1000)
        conn.execute(
            """INSERT INTO tiles(project_id, name,
                                 bbox_west, bbox_south, bbox_east, bbox_north,
                                 status, assigned_to, paused_at, data_png)
               VALUES (1,'raster_paused_tile',0,3.0,0.1,3.1,'in_progress',?,?,?)""",
            (op_paused, now, encode_mask(partial, 256)),
        )

        # Plain raster operators (project 1, role operator — never pulled into
        # the review queue) for the pause→start, heartbeat-recovery and
        # shortcut scenarios. Each gets its own user so tiles don't collide.
        for uname in ("op_pr", "op_hb", "op_keys"):
            conn.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at) "
                "VALUES (?,?,'operator',1,?)",
                (uname, hash_password("secret123"), now),
            )
            uid = conn.execute(
                "SELECT id FROM users WHERE username=?", (uname,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO project_members(project_id, user_id, role) VALUES (1,?,'operator')",
                (uid,),
            )

        # Two raster projects with different (remote, unreachable) imagery so
        # the project-switch scenario can assert the satellite map follows the
        # active project. Project 4 also configures a secondary (D) overlay.
        for proj_id, pname, primary, secondary in (
            (3, "raster_a", "https://imagery-a.invalid/{z}/{x}/{y}.png", None),
            (4, "raster_b", "https://imagery-b.invalid/{z}/{x}/{y}.png",
             "https://imagery-b2.invalid/{z}/{x}/{y}.png"),
        ):
            conn.execute(
                "INSERT INTO projects(id, name, description, kind, tile_px, meters_per_pixel, "
                "mask_complete_required, primary_mbtiles, secondary_mbtiles, active, created_at) "
                "VALUES (?,?,'','raster',256,2.5,1,?,?,1,?)",
                (proj_id, pname, primary, secondary, now),
            )
            for ordering, (cid, cname, color) in enumerate([
                (1, "Água", "#377eb8"), (2, "Edificado", "#e41a1c"),
            ]):
                conn.execute(
                    "INSERT INTO project_classes(project_id, class_id, name, color, ordering) "
                    "VALUES (?,?,?,?,?)",
                    (proj_id, cid, cname, color, ordering),
                )
            for i in range(3):
                conn.execute(
                    """INSERT INTO tiles(project_id, name,
                                         bbox_west, bbox_south, bbox_east, bbox_north,
                                         status, data_png)
                       VALUES (?,?,?,?,?,?,'pending',?)""",
                    (proj_id, f"{pname}_{i:03d}", i * 0.1, 4.0 + proj_id,
                     (i + 1) * 0.1, 4.1 + proj_id, empty),
                )
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) "
            "VALUES ('op_multi',?,'operator',1,?)",
            (hash_password("secret123"), now),
        )
        op_multi = conn.execute(
            "SELECT id FROM users WHERE username='op_multi'"
        ).fetchone()["id"]
        for proj_id in (3, 4):
            conn.execute(
                "INSERT INTO project_members(project_id, user_id, role) VALUES (?,?,'operator')",
                (proj_id, op_multi),
            )
    finally:
        conn.close()
    print("seeded")


if __name__ == "__main__":
    main(sys.argv[1])
