"""CLI: importa tiles a partir de pontos centrais (lat,lon).

Cada tile cobre tile_px×tile_px pixels com meters_per_pixel m/pixel = tile_meters
de lado no chão, lendo essas configurações do projeto (defaults: 256 px,
2.5 m/px → 640 m). O centro geodésico define a bbox via pyproj.Geod para que
o tamanho em metros seja constante em qualquer latitude. Tiles adjacentes
(--block) ficam exatamente colados (sem gaps nem sobreposição).

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
from backend import tile_ingest
from backend.scripts._common import resolve_project_arg


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=str, help="CSV com colunas lat,lon,name")
    ap.add_argument("--point", action="append", nargs="+",
                    help="lat lon [nome] (pode repetir). Ex.: --point -23.55 -46.63 centro")
    ap.add_argument("--block", type=int, default=1,
                    help="bloco NxN de tiles adjacentes (640m) ao redor de cada ponto. Ímpar. Default 1.")
    ap.add_argument("--project", type=str, default=None,
                    help="id ou nome do projeto que recebe os tiles. Obrigatório quando há mais de um.")
    args = ap.parse_args()

    if not args.csv and not args.point:
        ap.error("informe --csv e/ou --point")
    if args.block < 1 or args.block % 2 == 0:
        ap.error("--block deve ser ímpar (1, 3, 5, ...)")

    pts = _read_points(args)
    if not pts:
        print("nenhum ponto informado"); sys.exit(1)

    init_db()
    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project)
    finally:
        conn.close()

    # Shared with the admin UI endpoint (backend.tile_ingest) so both insert
    # tiles identically (geodesic bbox, per-project geometry, dedup).
    res = tile_ingest.add_points(project_id, pts, block=args.block)
    print(f"inseridos: {res['inserted']} · já existiam: {res['skipped']} · "
          f"cada tile = {res['tile_meters']:.0f}m × {res['tile_meters']:.0f}m "
          f"({res['tile_px']}×{res['tile_px']} px)")


if __name__ == "__main__":
    main()
