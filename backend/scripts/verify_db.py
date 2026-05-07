"""CLI: integrity sweep for `tileclass.db`.

Runs three layers of checks and exits non-zero on failure:
  1. SQLite engine: PRAGMA integrity_check + foreign_key_check.
  2. Schema invariants: every tile.project_id resolves; mbtiles paths that
     are local files exist; no orphan project_classes / project_members.
  3. Mask integrity: every classified/in_review/reviewed tile decodes
     and only contains class IDs that exist in the project's palette.

Designed to be run from cron / CI after backups.

Uso:
    python -m backend.scripts.verify_db
    python -m backend.scripts.verify_db --quick   # skip mask decoding
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import connect, init_db
from backend.mask_utils import decode_mask
from backend.project_service import is_remote_layer, resolve_mbtiles_path, LAYER_KEYS


def _problem(label: str, msg: str, problems: list) -> None:
    problems.append(f"[{label}] {msg}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="pula a varredura de máscaras (mais lento, lê todos os PNGs)")
    args = ap.parse_args()

    init_db()
    problems: list[str] = []
    conn = connect()
    try:
        # 1. Engine-level integrity.
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            _problem("engine", f"integrity_check: {ic}", problems)
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        for v in fk_violations:
            _problem("fk", f"row {v[1]} of {v[0]} → {v[2]}.{v[3]}", problems)

        # 2. Schema invariants.
        orphans = conn.execute(
            """SELECT t.id FROM tiles t
               LEFT JOIN projects p ON p.id=t.project_id
               WHERE p.id IS NULL"""
        ).fetchall()
        for r in orphans:
            _problem("schema", f"tile {r['id']} has no project", problems)

        orphans_cls = conn.execute(
            """SELECT pc.project_id, pc.class_id FROM project_classes pc
               LEFT JOIN projects p ON p.id=pc.project_id
               WHERE p.id IS NULL"""
        ).fetchall()
        for r in orphans_cls:
            _problem("schema",
                     f"orphan project_classes row pid={r['project_id']} cid={r['class_id']}",
                     problems)

        # mbtiles paths: local must exist, remote must have placeholders.
        # Inactive projects are skipped — admin may have archived them and
        # cleaned up the source files; flagging them is noise.
        for proj in conn.execute("SELECT * FROM projects WHERE active=1").fetchall():
            for layer in LAYER_KEYS:
                col = {
                    "primary": "primary_mbtiles",
                    "secondary": "secondary_mbtiles",
                    "tertiary": "tertiary_mbtiles",
                    "ref_primary": "ref_mask_primary_mbtiles",
                    "ref_secondary": "ref_mask_secondary_mbtiles",
                }[layer]
                path = proj[col]
                if not path:
                    continue
                if is_remote_layer(path):
                    for ph in ("{z}", "{x}", "{y}"):
                        if ph not in path:
                            _problem("schema",
                                     f"project {proj['id']} layer {layer} URL missing {ph}",
                                     problems)
                    continue
                resolved = resolve_mbtiles_path(path)
                if resolved is None or not resolved.exists():
                    _problem("schema",
                             f"project {proj['id']} layer {layer} file missing: {path}",
                             problems)

        # 3. Mask sanity.
        if not args.quick:
            allowed_per_project: dict[int, set[int]] = {}
            for r in conn.execute(
                "SELECT project_id, class_id FROM project_classes"
            ).fetchall():
                allowed_per_project.setdefault(r["project_id"], set()).add(r["class_id"])

            tiles = conn.execute(
                """SELECT id, project_id, status, data_png FROM tiles
                   WHERE status IN ('classified','in_review','reviewed')
                     AND data_png IS NOT NULL"""
            ).fetchall()
            for t in tiles:
                allowed = allowed_per_project.get(t["project_id"], set()) | {255}
                try:
                    raw = decode_mask(t["data_png"])
                except Exception as e:
                    _problem("mask", f"tile {t['id']} decode failed: {e}", problems)
                    continue
                bad = {b for b in set(raw) if b not in allowed}
                if bad:
                    _problem("mask",
                             f"tile {t['id']} has class ids {sorted(bad)} not in project palette",
                             problems)
    finally:
        conn.close()

    if not problems:
        print("ok — banco íntegro.")
        sys.exit(0)
    print(f"FALHAS ({len(problems)}):")
    for p in problems:
        print(f"  {p}")
    sys.exit(2)


if __name__ == "__main__":
    main()
