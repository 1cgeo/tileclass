"""CLI: importa os tiles selecionados para CQ a partir do geoparquet
`cq_selection.geoparquet` produzido pelo pipeline do correcao_mascaras.

Cada linha do geoparquet tem:
  - stem           (nome da imagem Sentinel-2)
  - x_off, y_off   (offset em pixels 2.39m no raster original)
  - geometry       (polígono WGS84 do tile 256×256)
  - cq_role, cq_score, ... (atributos auxiliares)

Pro banco da aplicação (schema `tiles`):
  - extraímos o centroide do polígono e chamamos `bbox_from_center`
    (bbox de 640m, padrão app — ligeiramente maior que os 612m do tile original);
  - name = `<stem>_x<x_off>_y<y_off>_<cq_role>`;
  - status='pending', máscara inicial **vazia** (tudo 255) por padrão (seed empty)
    — pra GT manual sem viés; opção `--seed raw` copia DSG bruto como rascunho.

Uso:
  python -m backend.scripts.import_cq_tiles \\
      --geoparquet C:/.../relatorio/dados/cq_selection.geoparquet \\
      [--seed {empty,raw}] [--raw-dir C:/.../teste_masks] [--dry-run]
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

# PROJ fix (evita proj.db do PostgreSQL/PostGIS)
_PY_SP = Path(sys.executable).parent / "Lib" / "site-packages"
os.environ.setdefault("PROJ_DATA", str(_PY_SP / "rasterio" / "proj_data"))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from rasterio.crs import CRS

from backend.database import init_db, connect
from backend.geo import bbox_from_center, TILE_PX
from backend.mask_utils import encode_mask, validate_submission
from backend.tile_grid import force_crs_3857

NODATA = 255
DEFAULT_RAW_DIR = Path(r"C:/Users/diniz/OneDrive/Desktop/Desenvolvimento/treinamento_6c/teste/teste_masks")


def empty_seed() -> bytes:
    """65536 bytes de nodata (255) — máscara vazia."""
    return bytes(bytearray([NODATA]) * (TILE_PX * TILE_PX))


def render_seed_from_raw_dsg(stem: str, bbox: tuple[float, float, float, float],
                             raw_dir: Path) -> bytes:
    """Reamostra o raw DSG (2.39m EPSG:3857) pra 256×256 cobrindo a bbox WGS84."""
    raw_path = raw_dir / f"mask_{stem}.tif"
    if not raw_path.exists():
        return empty_seed()
    west, south, east, north = bbox
    target_transform = from_bounds(west, south, east, north, TILE_PX, TILE_PX)
    target = np.full((TILE_PX, TILE_PX), NODATA, dtype=np.uint8)
    with rasterio.open(raw_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=target,
            src_transform=src.transform,
            src_crs=force_crs_3857(src.crs),
            dst_transform=target_transform,
            dst_crs=CRS.from_epsg(4326),
            resampling=Resampling.nearest,
            src_nodata=NODATA,
            dst_nodata=NODATA,
        )
    # DSG classes já são 0..5; mapeia pra convenção app (1..6).
    # 0=agua→1, 1=edif→2, 2=terr_exp→6, 3=campo→4, 4=floresta→3, 5=cultivada→5
    lut = np.full(256, NODATA, dtype=np.uint8)
    lut[0] = 1; lut[1] = 2; lut[2] = 6; lut[3] = 4; lut[4] = 3; lut[5] = 5
    lut[NODATA] = NODATA
    return lut[target].tobytes()


def tile_name(row) -> str:
    role = str(row.get("cq_role", "cq"))
    return f"{row['stem']}_x{int(row['x_off'])}_y{int(row['y_off'])}_{role}"


def _resolve_project(conn, project_arg: str | None) -> int:
    rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    if not rows:
        print("erro: nenhum projeto cadastrado.")
        sys.exit(1)
    if project_arg is None and len(rows) == 1:
        return rows[0]["id"]
    if project_arg is None:
        names = ", ".join(f"{r['id']}={r['name']}" for r in rows)
        print(f"--project é obrigatório (vários projetos): {names}")
        sys.exit(1)
    try:
        pid = int(project_arg)
        for r in rows:
            if r["id"] == pid:
                return pid
    except ValueError:
        pass
    for r in rows:
        if r["name"] == project_arg:
            return r["id"]
    print(f"projeto não encontrado: {project_arg}")
    sys.exit(1)


def insert_tile(conn, project_id: int, name: str,
                bbox: tuple[float, float, float, float], png: bytes) -> bool:
    west, south, east, north = bbox
    existing = conn.execute(
        """SELECT id FROM tiles
           WHERE project_id=?
             AND ABS(bbox_west - ?) < 1e-6 AND ABS(bbox_south - ?) < 1e-6
             AND ABS(bbox_east - ?) < 1e-6 AND ABS(bbox_north - ?) < 1e-6""",
        (project_id, west, south, east, north),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                             status, data_png)
           VALUES (?,?,?,?,?,?,'pending',?)""",
        (project_id, name, west, south, east, north, png),
    )
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--geoparquet", required=True, type=Path,
                    help="cq_selection.geoparquet")
    ap.add_argument("--seed", choices=["empty", "raw"], default="empty",
                    help="máscara inicial: 'empty' (255, padrão) ou 'raw' (DSG bruto)")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR,
                    help="diretório com mask_<stem>.tif do DSG bruto (para --seed raw)")
    ap.add_argument("--dry-run", action="store_true",
                    help="imprime sem tocar o DB")
    ap.add_argument("--project", type=str, default=None,
                    help="id ou nome do projeto que recebe os tiles. Obrigatório quando há mais de um.")
    args = ap.parse_args()

    if not args.geoparquet.exists():
        ap.error(f"geoparquet não encontrado: {args.geoparquet}")
    if args.seed == "raw" and not args.raw_dir.exists():
        ap.error(f"--raw-dir não existe: {args.raw_dir}")

    gdf = gpd.read_parquet(args.geoparquet)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        print(f"[info] reprojetando {gdf.crs} → EPSG:4326")
        gdf = gdf.to_crs(4326)
    print(f"[info] {len(gdf)} tiles em {args.geoparquet}; seed={args.seed}")

    if not args.dry_run:
        init_db()
        conn = connect()
        project_id = _resolve_project(conn, args.project)
        conn.execute("BEGIN")
    else:
        conn = None
        project_id = None

    inserted = skipped = errors = 0
    try:
        for i, row in enumerate(gdf.itertuples(index=False), 1):
            row_d = row._asdict()
            try:
                centroid = row_d["geometry"].centroid
                lon, lat = centroid.x, centroid.y
                bbox = bbox_from_center(lat, lon)
                name = tile_name(row_d)
                if args.seed == "empty":
                    raw = empty_seed()
                else:
                    raw = render_seed_from_raw_dsg(row_d["stem"], bbox, args.raw_dir)
                png = encode_mask(raw)
                n_filled = (TILE_PX * TILE_PX) - sum(1 for b in raw if b == NODATA)
                if i <= 5 or i % 50 == 0 or i == len(gdf):
                    print(f"  [{i}/{len(gdf)}] {name[:70]}...  fill={n_filled}/65536")
                if args.dry_run:
                    continue
                if insert_tile(conn, project_id, name, bbox, png):
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                print(f"  [{i}/{len(gdf)}] ERRO: {e}")
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
