"""CLI: importa tiles selecionados pelo BDF (dataset_cobertura) com a máscara
do BDF como **rascunho inicial** ao invés de máscara vazia.

Diferente de `import_points`:
  - aceita CSV com coluna extra `bdf_cell_id`;
  - lê o raster argmax do BDF (já em IDs do tileclass) e gera um seed mask
    256×256 reamostrado NN à bbox de cada tile;
  - converte nodata do BDF (0) → nodata do tileclass (255).

Uso:
    python -m backend.scripts.import_qc_tiles \\
        --csv qc_tiles.csv \\
        --bdf-dir C:/path/to/dataset_cobertura/outputs/fusion_grid_blend

Colunas esperadas no CSV: lat, lon, name, bdf_cell_id
Linhas existentes na tabela tiles (mesmo bbox, ~0.1 m) são puladas.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from backend.database import init_db, connect
from backend.geo import bbox_from_center, TILE_PX, TILE_METERS
from backend.mask_utils import encode_mask, validate_submission


def render_seed_from_bdf(
    lat: float, lon: float, bdf_argmax_path: Path,
) -> tuple[bytes, tuple[float, float, float, float], dict]:
    """Reamostra o BDF para 256×256 cobrindo a bbox geodésica do tile centrada
    em (lat, lon). Retorna (raw_bytes, bbox, info)."""
    west, south, east, north = bbox_from_center(lat, lon)
    target_transform = from_bounds(west, south, east, north, TILE_PX, TILE_PX)
    target = np.zeros((TILE_PX, TILE_PX), dtype=np.uint8)

    with rasterio.open(bdf_argmax_path) as src:
        # usa o CRS do src direto (já é EPSG:4326) — evita lookup do PROJ
        reproject(
            source=rasterio.band(src, 1),
            destination=target,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=target_transform, dst_crs=src.crs,
            resampling=Resampling.nearest,
        )

    # nodata BDF (0) → nodata tileclass (255)
    seed = np.where(target == 0, 255, target).astype(np.uint8)
    raw = seed.tobytes()

    # contagem por classe (sem nodata)
    counts = np.bincount(seed.ravel(), minlength=256)
    info = {
        "n_nodata": int(counts[255]),
        "n_filled": int((seed != 255).sum()),
        "class_counts": {int(c): int(counts[c]) for c in range(1, 7) if counts[c] > 0},
    }
    return raw, (west, south, east, north), info


def insert_tile(conn, name: str, bbox: tuple[float, float, float, float],
                png: bytes) -> bool:
    west, south, east, north = bbox
    existing = conn.execute(
        """SELECT id FROM tiles
           WHERE ABS(bbox_west - ?) < 1e-6 AND ABS(bbox_south - ?) < 1e-6
             AND ABS(bbox_east - ?) < 1e-6 AND ABS(bbox_north - ?) < 1e-6""",
        (west, south, east, north),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO tiles(name, bbox_west, bbox_south, bbox_east, bbox_north,
                             status, data_png)
           VALUES (?,?,?,?,?,'pending',?)""",
        (name, west, south, east, north, png),
    )
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--csv", required=True, type=Path,
                    help="CSV com colunas lat,lon,name,bdf_cell_id")
    ap.add_argument("--bdf-dir", required=True, type=Path,
                    help="diretório com {cell_id}_argmax.tif")
    ap.add_argument("--dry-run", action="store_true",
                    help="só imprime o que faria, sem tocar o DB")
    args = ap.parse_args()

    if not args.csv.exists():
        ap.error(f"CSV não encontrado: {args.csv}")
    if not args.bdf_dir.exists():
        ap.error(f"--bdf-dir não encontrado: {args.bdf_dir}")

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    print(f"Lendo {len(rows)} tiles de {args.csv}")

    if not args.dry_run:
        init_db()
        conn = connect()
        conn.execute("BEGIN")
    else:
        conn = None

    inserted = skipped = errors = 0
    try:
        for i, row in enumerate(rows, 1):
            lat = float(row["lat"]); lon = float(row["lon"])
            name = row["name"]
            cid = row["bdf_cell_id"]
            arg_path = args.bdf_dir / f"{cid}_argmax.tif"
            if not arg_path.exists():
                print(f"  [{i}/{len(rows)}] {name}  ERRO: {arg_path.name} não existe")
                errors += 1; continue
            raw, bbox, info = render_seed_from_bdf(lat, lon, arg_path)
            png = encode_mask(raw)
            ok, missing = validate_submission(raw)
            ncls = len(info["class_counts"])
            print(f"  [{i}/{len(rows)}] {name}  cls={ncls}  fill={info['n_filled']:>5}/65536"
                  f"  classes={info['class_counts']}")
            if args.dry_run:
                continue
            if insert_tile(conn, name, bbox, png):
                inserted += 1
            else:
                skipped += 1
        if conn:
            conn.execute("COMMIT")
    except Exception:
        if conn: conn.execute("ROLLBACK")
        raise
    finally:
        if conn: conn.close()

    print(f"\ninseridos: {inserted}  ·  já existiam: {skipped}  ·  erros: {errors}")
    if args.dry_run:
        print("(dry-run: nenhuma escrita no DB)")


if __name__ == "__main__":
    main()
