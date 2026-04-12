"""CLI: import tiles from CSV.

CSV columns (header required):
    name,bbox_west,bbox_south,bbox_east,bbox_north,zoom,tile_x,tile_y
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import init_db, connect
from backend.mask_utils import empty_mask_png


def main():
    if len(sys.argv) < 2:
        print("usage: python -m backend.scripts.import_tiles <csv_path>")
        sys.exit(1)
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"file not found: {csv_path}")
        sys.exit(1)

    init_db()
    empty = empty_mask_png()
    count = 0
    conn = connect()
    try:
        conn.execute("BEGIN")
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tx = int(row["tile_x"])
                ty = int(row["tile_y"])
                context = [[tx + dx, ty + dy]
                           for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                           if not (dx == 0 and dy == 0)]
                conn.execute(
                    """INSERT INTO tiles(name, bbox_west, bbox_south, bbox_east, bbox_north,
                       zoom, tile_x, tile_y, status, data_png, context_tiles)
                       VALUES (?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        row["name"],
                        float(row["bbox_west"]),
                        float(row["bbox_south"]),
                        float(row["bbox_east"]),
                        float(row["bbox_north"]),
                        int(row["zoom"]),
                        tx, ty,
                        empty,
                        json.dumps(context),
                    ),
                )
                count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"imported {count} tiles")


if __name__ == "__main__":
    main()
