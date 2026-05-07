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
from backend.geo import bbox_from_center
from backend.mask_utils import encode_mask, validate_submission
from backend.scripts._common import (
    resolve_project_arg, insert_tile_dedup, load_tile_geometry,
)


def render_seed_from_bdf(
    lat: float, lon: float, bdf_argmax_path: Path,
    *, tile_px: int, tile_meters: float,
) -> tuple[bytes, tuple[float, float, float, float], dict]:
    """Reamostra o BDF para tile_px×tile_px cobrindo a bbox geodésica do tile
    centrada em (lat, lon). Retorna (raw_bytes, bbox, info)."""
    west, south, east, north = bbox_from_center(lat, lon, tile_meters)
    target_transform = from_bounds(west, south, east, north, tile_px, tile_px)
    target = np.zeros((tile_px, tile_px), dtype=np.uint8)

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
    ap.add_argument("--project", type=str, default=None,
                    help="id ou nome do projeto que recebe os tiles. Obrigatório quando há mais de um.")
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
        project_id = resolve_project_arg(conn, args.project)
        conn.execute("BEGIN")
    else:
        conn = None
        project_id = None

    tile_px, tile_meters = load_tile_geometry(project_id)
    pixels = tile_px * tile_px

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
            raw, bbox, info = render_seed_from_bdf(
                lat, lon, arg_path, tile_px=tile_px, tile_meters=tile_meters,
            )
            png = encode_mask(raw, tile_px)
            ncls = len(info["class_counts"])
            print(f"  [{i}/{len(rows)}] {name}  cls={ncls}  fill={info['n_filled']:>5}/{pixels}"
                  f"  classes={info['class_counts']}")
            if args.dry_run:
                continue
            if insert_tile_dedup(conn, project_id, name, bbox, png):
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
