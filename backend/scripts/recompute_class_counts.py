"""CLI: backfill `tiles.class_counts` for classified/reviewed tiles whose
JSON cache is NULL (legacy tiles classified before the column existed).

Decodes each tile's PNG once and writes the pixel-count JSON. Idempotent:
tiles already populated are skipped unless --force is given.

Uso:
    python -m backend.scripts.recompute_class_counts            # backfill missing
    python -m backend.scripts.recompute_class_counts --force    # recompute all
    python -m backend.scripts.recompute_class_counts --project default
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import init_db, connect, transaction
from backend.mask_utils import decode_mask, class_counts
from backend.scripts._common import resolve_project_arg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="recomputa mesmo quando já há class_counts cacheados")
    ap.add_argument("--project", type=str, default=None,
                    help="id ou nome do projeto a backfillar (default: todos)")
    args = ap.parse_args()

    init_db()
    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project, allow_all=True)
        where = "status IN ('classified','in_review','reviewed') AND data_png IS NOT NULL"
        params: list = []
        if project_id is not None:
            where += " AND project_id=?"
            params.append(project_id)
        if not args.force:
            where += " AND class_counts IS NULL"
        rows = conn.execute(
            f"SELECT id, project_id, data_png FROM tiles WHERE {where} ORDER BY id",
            params,
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("Nada a fazer — nenhum tile elegível.")
        return

    # tile_px is per-project; decode with the right size or non-256 projects
    # would all fail to decode. Cache one lookup per project.
    from backend import project_service
    _px: dict[int, int] = {}

    def px_for(pid: int) -> int:
        if pid not in _px:
            _px[pid] = int((project_service.get_project(pid) or {}).get("tile_px", 256))
        return _px[pid]

    print(f"[info] backfill em {len(rows)} tile(s)...")
    updated = errors = 0
    with transaction("IMMEDIATE") as conn:
        for r in rows:
            try:
                raw = decode_mask(r["data_png"], px_for(r["project_id"]))
            except (ValueError, OSError) as e:
                errors += 1
                print(f"  [err] tile {r['id']}: {e}")
                continue
            cc = json.dumps(class_counts(raw), separators=(",", ":"))
            conn.execute("UPDATE tiles SET class_counts=? WHERE id=?", (cc, r["id"]))
            updated += 1
            if updated % 100 == 0:
                print(f"  [{updated}/{len(rows)}]")
    print(f"atualizados: {updated} · erros: {errors}")


if __name__ == "__main__":
    main()
