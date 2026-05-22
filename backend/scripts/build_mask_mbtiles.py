"""Gera um MBTiles overlay (PNG RGBA) a partir de máscaras EDGV 6c.

Schema MBTiles 1.3 padrão (idêntico ao build_mbtiles.py — só muda o conteúdo).

Para cada (z,x,y), reprojeta cada GeoTIFF que intersecta para o grid 3857
da tile com NEAREST (preserva IDs de classe), comporta como
"primeiro não-NoData vence" para sobrepor máscaras, colore com a paleta
do config.yaml (via EDGV_REMAP_LUT invertida) e salva PNG RGBA.

Uso:
  python -m backend.scripts.build_mask_mbtiles <src_dir> <out.mbtiles>
      [--zmin 10] [--zmax 17] [--workers 8] [--name DSG]

Convenção de classes (input EDGV 6c, igual treinamento_6c/<split>/<split>_masks/):
  0=agua, 1=edif, 2=terr_exp, 3=campo, 4=floresta, 5=veg_cultivada, 255=NoData
"""
from __future__ import annotations
import argparse
import io
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# PROJ fix (Windows tem 3 proj.db conflitantes — PostgreSQL/PostGIS,
# pyproj, rasterio). Detecta via importlib (funciona em venv e conda) e
# sobrescreve PROJ_DATA/PROJ_LIB — `setdefault` deixa um PROJ_LIB poluído
# system-wide (proj.db antigo do Postgres) e qualquer reproject quebra.
import importlib.util as _iu
_spec = _iu.find_spec("rasterio")
if _spec and _spec.origin:
    _RASTERIO_PROJ = Path(_spec.origin).parent / "proj_data"
    if _RASTERIO_PROJ.exists():
        os.environ["PROJ_DATA"] = str(_RASTERIO_PROJ)
        os.environ["PROJ_LIB"] = str(_RASTERIO_PROJ)

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import reproject

# Make backend/ importable when running as `python backend/scripts/build_mask_mbtiles.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.scripts.export_tiles import EDGV_REMAP_LUT
from backend.tile_grid import TILE, force_crs_3857, tile_bounds_3857, tiles_for_bbox, merc_to_lonlat

# Default TileClass palette (class id → #RRGGBB) used to color the EDGV-id
# rasters via EDGV_REMAP_LUT. Self-contained because config.yaml no longer
# carries classes (they're per-project domain data in the DB). Pass --project
# to pull live colors from a specific project's classes instead.
DEFAULT_PALETTE = {
    1: "#377eb8", 2: "#e41a1c", 3: "#4daf4a",
    4: "#ffff33", 5: "#984ea3", 6: "#ff7f00",
}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ------------- Color LUT (EDGV id → RGBA from config.yaml) -------------

def build_rgba_lut(palette: dict[int, str]) -> np.ndarray:
    """LUT (256, 4) uint8 mapping EDGV class id → RGBA. NoData/unknown → transparent.
    `palette` maps TileClass id (1..6) → #RRGGBB."""
    lut = np.zeros((256, 4), dtype=np.uint8)
    for tc_id, color in palette.items():
        edgv_id = int(EDGV_REMAP_LUT[int(tc_id)])  # EDGV id 0..5
        rgb = bytes.fromhex(color.lstrip("#"))
        lut[edgv_id] = (rgb[0], rgb[1], rgb[2], 255)
    return lut


def _resolve_palette(project_arg: str | None) -> dict[int, str]:
    """DEFAULT_PALETTE, or a specific project's class colors when --project is given."""
    if not project_arg:
        return DEFAULT_PALETTE
    from backend.database import connect
    from backend.scripts._common import resolve_project_arg
    conn = connect()
    try:
        pid = resolve_project_arg(conn, project_arg)
        rows = conn.execute(
            "SELECT class_id, color FROM project_classes WHERE project_id=? ORDER BY ordering",
            (pid,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        sys.exit(f"[err] projeto {project_arg} não tem classes")
    return {int(r[0]): r[1] for r in rows}


# ------------- Rendering -------------

def render_tile(args):
    """Renderiza (z,x,y) a partir de uma lista de (fonte, bbox_3857); retorna (z,x,y,png_bytes|None).

    Cada fonte vem com bbox já em EPSG:3857 (calculado no main) para que o
    early-out funcione mesmo quando o GeoTIFF nativo não está em 3857 (UTM, p.ex.).
    """
    sources, z, x, y, lut_bytes = args
    lut = np.frombuffer(lut_bytes, dtype=np.uint8).reshape(256, 4)
    west, south, east, north = tile_bounds_3857(z, x, y)
    res = (east - west) / TILE
    dst_transform = rasterio.transform.from_origin(west, north, res, res)

    out_ids = np.full((TILE, TILE), 255, dtype=np.uint8)  # 255 = NoData

    for src_path, (sw, ss, se, sn) in sources:
        if east <= sw or west >= se or north <= ss or south >= sn:
            continue
        with rasterio.open(src_path) as src:
            src_crs = force_crs_3857(src.crs)
            tmp = np.full((TILE, TILE), 255, dtype=np.uint8)
            reproject(
                source=rasterio.band(src, 1),
                destination=tmp,
                src_crs=src_crs,
                src_transform=src.transform,
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.nearest,
                src_nodata=255,
                dst_nodata=255,
            )
            fill = (out_ids == 255) & (tmp != 255)
            if fill.any():
                out_ids[fill] = tmp[fill]

    if (out_ids == 255).all():
        return z, x, y, None

    rgba = lut[out_ids]  # (TILE, TILE, 4)
    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return z, x, y, buf.getvalue()


# ------------- MBTiles IO (idêntico a build_mbtiles.py) -------------

MBTILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER NOT NULL,
    tile_column INTEGER NOT NULL,
    tile_row INTEGER NOT NULL,
    tile_data BLOB NOT NULL,
    PRIMARY KEY (zoom_level, tile_column, tile_row)
);
CREATE INDEX IF NOT EXISTS tiles_index ON tiles (zoom_level, tile_column, tile_row);
"""


def open_mbtiles(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.executescript(MBTILES_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def upsert_metadata(conn: sqlite3.Connection, items: dict[str, str]) -> None:
    for k, v in items.items():
        conn.execute(
            "INSERT INTO metadata(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (k, str(v)),
        )


def insert_tile(conn: sqlite3.Connection, z: int, x: int, y: int, data: bytes) -> None:
    tms_y = (2 ** z - 1) - y
    conn.execute(
        "INSERT OR IGNORE INTO tiles(zoom_level,tile_column,tile_row,tile_data) VALUES(?,?,?,?)",
        (z, x, tms_y, data),
    )


def existing_xyz_for_zoom(conn: sqlite3.Connection, z: int) -> set[tuple[int, int]]:
    n_minus_1 = (1 << z) - 1
    return {
        (x, n_minus_1 - tms_y)
        for x, tms_y in conn.execute(
            "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,)
        )
    }


# ------------- Main -------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src_dir", help="diretório com GeoTIFFs de máscara EDGV 6c")
    ap.add_argument("out_mbtiles", help="caminho do .mbtiles de saída")
    ap.add_argument("--zmin", type=int, default=10)
    ap.add_argument("--zmax", type=int, default=17)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--name", default="DSG")
    ap.add_argument("--description", default="DSG: EDGV 6c predictions (test split) colored with TileClass palette")
    ap.add_argument("--resume", action="store_true", help="pula tiles já presentes (idempotente)")
    ap.add_argument("--project", default=None,
                    help="id ou nome do projeto cujas cores usar (default: paleta TileClass padrão)")
    args = ap.parse_args()

    src_dir = Path(args.src_dir).resolve()
    out_path = Path(args.out_mbtiles).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tifs = sorted(src_dir.glob("*.tif"))
    if not tifs:
        sys.exit(f"[err] nenhum .tif em {src_dir}")
    print(f"[info] {len(tifs)} GeoTIFFs em {src_dir}")

    lut = build_rgba_lut(_resolve_palette(args.project))
    lut_bytes = lut.tobytes()
    print(f"[info] LUT EDGV→RGBA: " + ", ".join(
        f"{i}={tuple(lut[i])}" for i in range(6)
    ))

    # Coleta bounds globais em 3857
    global_bounds_3857 = None
    src_bounds: list[tuple[Path, tuple[float, float, float, float]]] = []
    from rasterio.warp import transform_bounds
    for p in tifs:
        with rasterio.open(p) as ds:
            src_crs = force_crs_3857(ds.crs)
            b = ds.bounds
            if src_crs.to_epsg() == 3857:
                bb = (b.left, b.bottom, b.right, b.top)
            else:
                bb = transform_bounds(src_crs, "EPSG:3857", b.left, b.bottom, b.right, b.top, densify_pts=21)
            src_bounds.append((p, bb))
            if global_bounds_3857 is None:
                global_bounds_3857 = list(bb)
            else:
                global_bounds_3857[0] = min(global_bounds_3857[0], bb[0])
                global_bounds_3857[1] = min(global_bounds_3857[1], bb[1])
                global_bounds_3857[2] = max(global_bounds_3857[2], bb[2])
                global_bounds_3857[3] = max(global_bounds_3857[3], bb[3])

    west_m, south_m, east_m, north_m = global_bounds_3857
    west_lon, south_lat = merc_to_lonlat(west_m, south_m)
    east_lon, north_lat = merc_to_lonlat(east_m, north_m)
    center_lon = (west_lon + east_lon) / 2
    center_lat = (south_lat + north_lat) / 2
    print(f"[info] bounds WGS84: {west_lon:.4f},{south_lat:.4f},{east_lon:.4f},{north_lat:.4f}")

    conn = open_mbtiles(out_path)
    upsert_metadata(conn, {
        "name": args.name,
        "format": "png",
        "type": "overlay",
        "version": "1",
        "description": args.description,
        "bounds": f"{west_lon:.6f},{south_lat:.6f},{east_lon:.6f},{north_lat:.6f}",
        "center": f"{center_lon:.6f},{center_lat:.6f},{args.zmin}",
        "minzoom": args.zmin,
        "maxzoom": args.zmax,
        "scheme": "xyz",
    })

    t_start = time.time()
    total_rendered = total_empty = total_skip = 0

    for z in range(args.zmin, args.zmax + 1):
        tile_sources: dict[tuple[int, int], list[tuple[Path, tuple[float, float, float, float]]]] = {}
        for p, bb in src_bounds:
            l, bt, r, t = bb
            xmin, ymin, xmax, ymax = tiles_for_bbox(z, l, bt, r, t)
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    tile_sources.setdefault((x, y), []).append((p, bb))

        candidates = list(tile_sources.items())
        n_cand = len(candidates)
        avg_src = np.mean([len(v) for v in tile_sources.values()]) if tile_sources else 0.0
        print(f"[z={z}] {n_cand:,} tiles candidatos, avg {avg_src:.1f} fontes/tile")

        if args.resume:
            already = existing_xyz_for_zoom(conn, z)
            filt = [(xy, srcs) for xy, srcs in candidates if xy not in already]
            skipped = n_cand - len(filt)
            total_skip += skipped
            candidates = filt
            print(f"[z={z}] resume: {skipped:,} já renderizados, processando {len(candidates):,}")
            if not candidates:
                continue

        jobs = [([(str(p), bb) for p, bb in srcs], z, x, y, lut_bytes) for (x, y), srcs in candidates]

        n_ok = n_empty = 0
        total = len(jobs)
        t_z = time.time()
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for start in range(0, total, args.batch_size):
                batch = jobs[start:start + args.batch_size]
                futs = [ex.submit(render_tile, j) for j in batch]
                conn.execute("BEGIN")
                for f in as_completed(futs):
                    tz, tx, ty, data = f.result()
                    if data is None:
                        n_empty += 1
                    else:
                        insert_tile(conn, tz, tx, ty, data)
                        n_ok += 1
                conn.execute("COMMIT")
                del futs
                done = start + len(batch)
                dt = time.time() - t_z
                rate = done / dt if dt > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  z={z}: {done}/{total}  ok={n_ok} empty={n_empty}  {rate:.0f} t/s  ETA {eta/60:.1f}m", flush=True)
        total_rendered += n_ok
        total_empty += n_empty
        print(f"[z={z}] ok={n_ok:,} empty={n_empty:,} em {(time.time()-t_z)/60:.1f} min")

    conn.execute("ANALYZE")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    conn = sqlite3.connect(str(out_path))
    total_tiles = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    size_mb = out_path.stat().st_size / (1024 * 1024)
    per_z = dict(conn.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall())
    conn.close()

    print(f"\n[done] {out_path}  {size_mb:.1f} MB  {total_tiles:,} tiles em {(time.time()-t_start)/60:.1f} min")
    print(f"  rendered: {total_rendered:,}  empty: {total_empty:,}  skipped(resume): {total_skip:,}")
    print(f"  per zoom: {per_z}")


if __name__ == "__main__":
    main()
