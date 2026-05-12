"""CLI: export classification tiles as a single CSV.

Output is one row per tile with `tile_id, project_id, name, class_id,
class_name, bbox_*, status, classified_by, reviewed_by, classified_at,
reviewed_at`. Operator/reviewer usernames are resolved via JOIN.

Usage:
    python -m backend.scripts.export_classifications <out_dir> [--status ...] [--project ...] [--manifest PATH]
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import connect
from backend.scripts._common import resolve_project_arg


STATUS_FILTERS = {
    "reviewed": ("reviewed",),
    "reviewed+classified": ("reviewed", "classified"),
}


HEADER = [
    "tile_id", "project_id", "name", "status",
    "class_id", "class_name", "class_color",
    "classified_by", "reviewed_by",
    "classified_at", "reviewed_at",
    "bbox_west", "bbox_south", "bbox_east", "bbox_north",
]


def _select_rows(statuses: tuple[str, ...], project_id: int | None) -> list:
    placeholders = ",".join("?" for _ in statuses)
    extra = "" if project_id is None else " AND t.project_id=?"
    args = list(statuses) + ([] if project_id is None else [project_id])
    conn = connect()
    try:
        return conn.execute(
            f"""SELECT t.id AS tile_id, t.project_id, t.name, t.status,
                       t.data_class_id AS class_id,
                       pc.name AS class_name, pc.color AS class_color,
                       uc.username AS classified_by,
                       ur.username AS reviewed_by,
                       t.classified_at, t.reviewed_at,
                       t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north
                FROM tiles t
                JOIN projects p ON p.id=t.project_id
                LEFT JOIN project_classes pc
                  ON pc.project_id=t.project_id AND pc.class_id=t.data_class_id
                LEFT JOIN users uc ON uc.id=t.classified_by
                LEFT JOIN users ur ON ur.id=t.reviewed_by
                WHERE p.kind='classification' AND t.status IN ({placeholders})
                  AND t.data_class_id IS NOT NULL{extra}
                ORDER BY t.project_id, t.name""",
            args,
        ).fetchall()
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("out_dir")
    p.add_argument("--status", choices=sorted(STATUS_FILTERS), default="reviewed")
    p.add_argument("--manifest", default=None,
                   help="manifest CSV path (default: <out_dir>/classifications.csv)")
    p.add_argument("--project", type=str, default=None,
                   help="id ou nome do projeto. Omitir = todos os classification.")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Path(args.manifest) if args.manifest else out_dir / "classifications.csv"
    statuses = STATUS_FILTERS[args.status]
    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project, allow_all=True)
    finally:
        conn.close()

    rows = _select_rows(statuses, project_id)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r[col] for col in HEADER])
    print(f"exported {len(rows)} classification tiles ({args.status}) to {manifest}")


if __name__ == "__main__":
    main()
