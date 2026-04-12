"""CLI: importa tiles a partir de pontos centrais (lat,lon).

Cada tile é sempre 256x256 pixels com 2.5 m/pixel = 640 m × 640 m no chão,
definido apenas pelo centro geodésico — NÃO usa zoom XYZ. A bbox é calculada
com pyproj.Geod para que o tamanho em metros seja constante em qualquer
latitude. Tiles adjacentes (--block) ficam exatamente colados (sem gaps nem
sobreposição).

Uso:
    # Um ponto isolado
    python -m backend.scripts.import_points --point -23.550 -46.633 centro_sp

    # Vários pontos de um CSV (colunas: lat,lon,name)
    python -m backend.scripts.import_points --csv pontos.csv

    # Bloco NxN colado ao redor de cada ponto (ímpar). 3 = centro + 8 vizinhos.
    python -m backend.scripts.import_points --csv pontos.csv --block 3
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import init_db, connect
from backend.mask_utils import empty_mask_png
from backend.geo import bbox_from_center, offset_center, TILE_METERS


def _read_points(args) -> list[tuple[float, float, str]]:
    pts: list[tuple[float, float, str]] = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                lat = float(row["lat"]); lon = float(row["lon"])
                name = row.get("name") or f"p{i}"
                pts.append((lat, lon, name))
    for p in (args.point or []):
        if len(p) < 2:
            print(f"--point requer lat lon [nome]: {p}"); sys.exit(1)
        lat, lon = float(p[0]), float(p[1])
        name = p[2] if len(p) >= 3 else f"{lat:.4f}_{lon:.4f}"
        pts.append((lat, lon, name))
    return pts


def _insert_tile(conn, name: str, lat_c: float, lon_c: float, empty_png: bytes) -> bool:
    west, south, east, north = bbox_from_center(lat_c, lon_c)
    # Idempotência: mesmo centro (com tolerância ~1 micrograu ≈ 0.1m) → skip.
    existing = conn.execute(
        """SELECT id FROM tiles
           WHERE ABS(bbox_west  - ?) < 1e-6
             AND ABS(bbox_south - ?) < 1e-6
             AND ABS(bbox_east  - ?) < 1e-6
             AND ABS(bbox_north - ?) < 1e-6""",
        (west, south, east, north),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO tiles(name, bbox_west, bbox_south, bbox_east, bbox_north,
                             status, data_png)
           VALUES (?,?,?,?,?,'pending',?)""",
        (name, west, south, east, north, empty_png),
    )
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=str, help="CSV com colunas lat,lon,name")
    ap.add_argument("--point", action="append", nargs="+",
                    help="lat lon [nome] (pode repetir). Ex.: --point -23.55 -46.63 centro")
    ap.add_argument("--block", type=int, default=1,
                    help="bloco NxN de tiles adjacentes (640m) ao redor de cada ponto. Ímpar. Default 1.")
    args = ap.parse_args()

    if not args.csv and not args.point:
        ap.error("informe --csv e/ou --point")
    if args.block < 1 or args.block % 2 == 0:
        ap.error("--block deve ser ímpar (1, 3, 5, ...)")

    pts = _read_points(args)
    if not pts:
        print("nenhum ponto informado"); sys.exit(1)

    init_db()
    empty_png = empty_mask_png()
    radius = args.block // 2
    inserted = skipped = 0

    conn = connect()
    try:
        conn.execute("BEGIN")
        for lat, lon, name in pts:
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    lat_c, lon_c = offset_center(lat, lon, dx, dy)
                    tname = name if args.block == 1 else f"{name}_{dx:+d}{dy:+d}"
                    if _insert_tile(conn, tname, lat_c, lon_c, empty_png):
                        inserted += 1
                    else:
                        skipped += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    print(f"inseridos: {inserted} · já existiam: {skipped} · cada tile = {TILE_METERS:.0f}m × {TILE_METERS:.0f}m")


if __name__ == "__main__":
    main()
