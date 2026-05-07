"""CLI: mescla um banco TileClass secundário no primário.

Políticas (confirmadas):
  - Users com MESMO username → considerar mesma pessoa. Usa id do primário,
    mantém atributos do primário (senha/role/active/can_review).
  - Tiles com MESMA bbox → primário vence (secundário é skipado; action_logs
    apontando pra ele também).
  - action_log → migrar tudo (menos os órfãos de tile skipado).
  - Tiles in_progress/in_review → resetam pra pending, assigned_to=NULL
    (sessão do secundário não existe no primário).
  - Não mexe em rate_limit / token_blacklist / sqlite_sequence (transientes).

Backup automático do primário antes de escrever. Suporta --dry-run.

Uso:
  python -m backend.scripts.merge_db --primary backend/tileclass.db \\
      --secondary /path/to/other/tileclass.db [--dry-run]
"""
from __future__ import annotations
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.database import connect, transaction

# bbox tolerance: floats podem variar por 1ulp entre instâncias; tolera 1e-6 deg ~0.1m
BBOX_EPS = 1e-6

RESUMABLE_STATUSES = {"in_progress", "in_review"}


def bbox_key(w: float, s: float, e: float, n: float) -> tuple[int, int, int, int]:
    """Hashable bucket that groups bboxes within BBOX_EPS."""
    return (
        round(w / BBOX_EPS),
        round(s / BBOX_EPS),
        round(e / BBOX_EPS),
        round(n / BBOX_EPS),
    )


def build_user_map(pri: sqlite3.Connection, sec: sqlite3.Connection,
                   ) -> tuple[dict[int, int], int, int]:
    """Insere users novos; retorna (user_map, n_reused, n_inserted)."""
    pri_usernames = {row[1]: row[0] for row in pri.execute("SELECT id, username FROM users")}
    user_map: dict[int, int] = {}
    reused = inserted = 0
    for row in sec.execute(
        "SELECT id, username, password_hash, role, active, created_at, can_review FROM users"
    ):
        sid, username, pwd, role, active, created_at, can_review = row
        if username in pri_usernames:
            user_map[sid] = pri_usernames[username]
            reused += 1
        else:
            cur = pri.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at, can_review) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, pwd, role, active, created_at, can_review),
            )
            user_map[sid] = cur.lastrowid
            pri_usernames[username] = cur.lastrowid
            inserted += 1
    return user_map, reused, inserted


def build_project_map(pri: sqlite3.Connection, sec: sqlite3.Connection,
                      user_map: dict[int, int],
                      ) -> tuple[dict[int, int], int, int]:
    """Map secondary projects → primary project ids.
       - Same-name match → reuse the primary project (paths/classes/members
         from the primary win; secondary metadata is ignored).
       - No match → insert a new project, copying classes and members from
         the secondary (with users remapped through user_map).
    Returns (project_map, n_reused, n_inserted)."""
    pri_by_name = {r["name"]: r["id"] for r in pri.execute(
        "SELECT id, name FROM projects"
    ).fetchall()}
    project_map: dict[int, int] = {}
    reused = inserted = 0
    sec.row_factory = sqlite3.Row
    sec_projects = sec.execute("SELECT * FROM projects").fetchall()
    for sp in sec_projects:
        sid = sp["id"]
        if sp["name"] in pri_by_name:
            project_map[sid] = pri_by_name[sp["name"]]
            reused += 1
            continue
        # Insert new project; created_by may be a secondary user id we just
        # mapped, otherwise NULL.
        created_by = user_map.get(sp["created_by"]) if sp["created_by"] else None
        cur = pri.execute(
            """INSERT INTO projects(name, description, mask_complete_required,
               primary_mbtiles, secondary_mbtiles, tertiary_mbtiles,
               ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
               active, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (sp["name"], sp["description"], sp["mask_complete_required"],
             sp["primary_mbtiles"], sp["secondary_mbtiles"], sp["tertiary_mbtiles"],
             sp["ref_mask_primary_mbtiles"], sp["ref_mask_secondary_mbtiles"],
             sp["active"], created_by, sp["created_at"]),
        )
        new_pid = cur.lastrowid
        project_map[sid] = new_pid
        pri_by_name[sp["name"]] = new_pid
        # Copy classes verbatim.
        for cl in sec.execute(
            "SELECT class_id, name, color, ordering FROM project_classes WHERE project_id=?",
            (sid,),
        ):
            pri.execute(
                """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
                   VALUES(?,?,?,?,?)""",
                (new_pid, cl["class_id"], cl["name"], cl["color"], cl["ordering"]),
            )
        # Copy memberships through user_map.
        for m in sec.execute(
            "SELECT user_id, role FROM project_members WHERE project_id=?", (sid,)
        ):
            mapped = user_map.get(m["user_id"])
            if mapped is not None:
                pri.execute(
                    "INSERT OR IGNORE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
                    (new_pid, mapped, m["role"]),
                )
        inserted += 1
    return project_map, reused, inserted


def build_tile_map(pri: sqlite3.Connection, sec: sqlite3.Connection,
                   user_map: dict[int, int],
                   project_map: dict[int, int],
                   ) -> tuple[dict[int, int], int, int, int]:
    """Insere tiles novos; retorna (tile_map, n_new, n_skipped, n_reset).
    Tiles in_progress/in_review viram pending. Uniqueness is per (project, bbox),
    so two projects covering the same area both keep their tiles."""
    pri_bboxes: dict[tuple[int, tuple], int] = {}
    for row in pri.execute(
        "SELECT id, project_id, bbox_west, bbox_south, bbox_east, bbox_north FROM tiles"
    ):
        pri_bboxes[(row[1], bbox_key(row[2], row[3], row[4], row[5]))] = row[0]

    cols = ("project_id", "name", "bbox_west", "bbox_south", "bbox_east", "bbox_north",
            "status", "assigned_to", "classified_by", "reviewed_by",
            "classified_at", "reviewed_at", "data_png", "problem_note",
            "version", "paused_at")
    placeholders = ", ".join("?" for _ in cols)
    insert_sql = f"INSERT INTO tiles({', '.join(cols)}) VALUES ({placeholders})"

    tile_map: dict[int, int] = {}
    new = skipped = reset = 0
    for row in sec.execute(f"SELECT id, {', '.join(cols)} FROM tiles"):
        sid = row[0]
        vals = dict(zip(cols, row[1:]))
        # Remap project id.
        sec_pid = vals["project_id"]
        new_pid = project_map.get(sec_pid)
        if new_pid is None:
            # Project couldn't be mapped (shouldn't happen — every secondary
            # tile must reference a project that exists). Skip defensively.
            skipped += 1
            continue
        vals["project_id"] = new_pid

        key = (new_pid, bbox_key(vals["bbox_west"], vals["bbox_south"],
                                  vals["bbox_east"], vals["bbox_north"]))
        if key in pri_bboxes:
            skipped += 1
            continue

        for fk in ("assigned_to", "classified_by", "reviewed_by"):
            if vals[fk] is not None:
                vals[fk] = user_map.get(vals[fk])

        if vals["status"] in RESUMABLE_STATUSES:
            vals["status"] = "pending"
            vals["assigned_to"] = None
            vals["paused_at"] = None
            reset += 1

        cur = pri.execute(insert_sql, tuple(vals[c] for c in cols))
        tile_map[sid] = cur.lastrowid
        pri_bboxes[key] = cur.lastrowid
        new += 1

    return tile_map, new, skipped, reset


def migrate_action_log(pri: sqlite3.Connection, sec: sqlite3.Connection,
                       user_map: dict[int, int], tile_map: dict[int, int],
                       ) -> tuple[int, int]:
    """Retorna (migrated, orphaned). Logs com tile skipado ou user órfão são descartados."""
    migrated = orphaned = 0
    for row in sec.execute(
        "SELECT user_id, tile_id, action, detail, created_at FROM action_log"
    ):
        sec_user_id, sec_tile_id, action, detail, created_at = row
        new_user = user_map.get(sec_user_id)
        if new_user is None:
            orphaned += 1
            continue
        if sec_tile_id is None:
            new_tile = None  # login/logout sem tile
        else:
            new_tile = tile_map.get(sec_tile_id)
            if new_tile is None:
                orphaned += 1
                continue
        pri.execute(
            "INSERT INTO action_log(user_id, tile_id, action, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_user, new_tile, action, detail, created_at),
        )
        migrated += 1
    return migrated, orphaned


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--primary", required=True, type=Path, help="DB principal (será escrito)")
    ap.add_argument("--secondary", required=True, type=Path, help="DB a importar (somente leitura)")
    ap.add_argument("--dry-run", action="store_true", help="simula sem alterar nada")
    args = ap.parse_args()

    if not args.primary.exists():
        ap.error(f"primary não encontrado: {args.primary}")
    if not args.secondary.exists():
        ap.error(f"secondary não encontrado: {args.secondary}")
    if args.primary.resolve() == args.secondary.resolve():
        ap.error("primary e secondary apontam pro mesmo arquivo")

    if not args.dry_run:
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = args.primary.with_suffix(args.primary.suffix + f".bak-{ts}")
        shutil.copy2(args.primary, backup)
        print(f"[backup] {backup}")

    sec = sqlite3.connect(f"file:{args.secondary.as_posix()}?mode=ro", uri=True)
    try:
        if args.dry_run:
            # Abre transação manual e faz ROLLBACK — insere de verdade durante o
            # dry-run para reportar números reais (FKs válidas, autoincrement),
            # depois descarta tudo atomicamente.
            pri = connect(args.primary)
            try:
                pri.execute("BEGIN IMMEDIATE")
                _run(pri, sec)
                pri.execute("ROLLBACK")
                print("[dry-run] nenhuma escrita aplicada")
            finally:
                pri.close()
        else:
            with transaction(path=args.primary) as pri:
                _run(pri, sec)
            print("[commit] merge aplicado")
    finally:
        sec.close()


def _run(pri: sqlite3.Connection, sec: sqlite3.Connection) -> None:
    user_map, u_reused, u_new = build_user_map(pri, sec)
    print(f"[users] {u_new} novos, {u_reused} reusados (username ja existia)")

    project_map, p_reused, p_new = build_project_map(pri, sec, user_map)
    print(f"[projects] {p_new} novos, {p_reused} reusados (mesmo nome)")

    tile_map, t_new, t_skipped, t_reset = build_tile_map(pri, sec, user_map, project_map)
    print(f"[tiles] {t_new} novos, {t_skipped} skipados (bbox duplicada por projeto), "
          f"{t_reset} resetados (in_progress/in_review -> pending)")

    log_new, log_orphan = migrate_action_log(pri, sec, user_map, tile_map)
    print(f"[action_log] {log_new} migrados, {log_orphan} descartados (tile skipado ou user orfao)")


if __name__ == "__main__":
    main()
