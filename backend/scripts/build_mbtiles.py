"""Gera um MBTiles unificado (WebP) a partir de GeoTIFFs em EPSG:3857.

Schema MBTiles 1.3 padrão:
  - metadata(name TEXT, value TEXT)
  - tiles(zoom_level, tile_column, tile_row, tile_data) -- tile_row em TMS

Uso:
  python -m backend.scripts.build_mbtiles <src_dir> <out.mbtiles>
      [--zmin 11] [--zmax 16] [--quality 85] [--workers 8]

Rendering: por (z,x,y), reprojeta cada GeoTIFF que intersecta pro grid da
tile (256x256 3857) via rasterio.warp.reproject, componde as fontes
(primeiro-que-preencher-pixel vence via alpha) e salva WebP.

Concorrência: workers renderizam tiles em paralelo (ProcessPool); o
main thread serializa INSERTs no SQLite (single-writer).
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

# PROJ fix (Windows tem 3 proj.db conflitantes)
_PY_SP = Path(sys.executable).parent / "Lib" / "site-packages"
os.environ.setdefault("PROJ_DATA", str(_PY_SP / "rasterio" / "proj_data"))

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import reproject

# Make backend/ importable when running as `python backend/scripts/build_mbtiles.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tile_grid import TILE, force_crs_3857, tile_bounds_3857, tiles_for_bbox, merc_to_lonlat

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ------------- Rendering -------------

def render_tile(args):
    """Renderiza (z,x,y) a partir de uma lista de fontes; retorna (z,x,y,webp_bytes|None)."""
    sources, z, x, y, quality = args
    west, south, east, north = tile_bounds_3857(z, x, y)
    res = (east - west) / TILE
    dst_transform = rasterio.transform.from_origin(west, north, res, res)

    rgb = np.zeros((3, TILE, TILE), dtype=np.uint8)
    alpha = np.zeros((TILE, TILE), dtype=np.uint8)

    for src_path in sources:
        with rasterio.open(src_path) as src:
            sb = src.bounds
            if east <= sb.left or west >= sb.right or north <= sb.bottom or south >= sb.top:
                continue

            src_crs = force_crs_3857(src.crs)
            tmp = np.zeros((3, TILE, TILE), dtype=np.uint8)
            for i in range(3):
                reproject(
                    source=rasterio.band(src, i + 1),
                    destination=tmp[i],
                    src_crs=src_crs,
                    src_transform=src.transform,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:3857",
                    resampling=Resampling.bilinear,
                )
            src_alpha = (tmp.sum(axis=0) > 0).astype(np.uint8) * 255
            mask_new = (alpha == 0) & (src_alpha > 0)
            if mask_new.any():
                for i in range(3):
                    rgb[i][mask_new] = tmp[i][mask_new]
                alpha[mask_new] = src_alpha[mask_new]

    if alpha.sum() == 0:
        return z, x, y, None

    arr = np.transpose(rgb, (1, 2, 0))
    rgba = np.dstack([arr, alpha])
    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=4)
    return z, x, y, buf.getvalue()


# ------------- MBTiles IO -------------

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
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; we manage txn
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
    # XYZ -> TMS: inverte y
    tms_y = (2 ** z - 1) - y
    conn.execute(
        "INSERT OR IGNORE INTO tiles(zoom_level,tile_column,tile_row,tile_data) VALUES(?,?,?,?)",
        (z, x, tms_y, data),
    )


def existing_xyz_for_zoom(conn: sqlite3.Connection, z: int) -> set[tuple[int, int]]:
    """Return {(x, y)} in XYZ convention for all tiles already at this zoom."""
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
    ap.add_argument("src_dir", help="diretório com GeoTIFFs 3857")
    ap.add_argument("out_mbtiles", help="caminho do .mbtiles de saída")
    ap.add_argument("--zmin", type=int, default=11)
    ap.add_argument("--zmax", type=int, default=16)
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--batch-size", type=int, default=2000,
                    help="tiles por batch (memória ≈ batch × webp_size; default 2000 ≈ 40 MB)")
    ap.add_argument("--name", default="teste_imagens")
    ap.add_argument("--resume", action="store_true",
                    help="pula tiles já presentes no .mbtiles (idempotente)")
    args = ap.parse_args()

    src_dir = Path(args.src_dir).resolve()
    out_path = Path(args.out_mbtiles).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tifs = sorted(src_dir.glob("*.tif"))
    if not tifs:
        sys.exit(f"[err] nenhum .tif em {src_dir}")
    print(f"[info] {len(tifs)} GeoTIFFs em {src_dir}")

    # Coleta bounds globais (em 3857)
    global_bounds_3857 = None
    src_bounds: list[tuple[Path, tuple[float, float, float, float]]] = []
    for p in tifs:
        with rasterio.open(p) as ds:
            b = ds.bounds
            src_bounds.append((p, (b.left, b.bottom, b.right, b.top)))
            if global_bounds_3857 is None:
                global_bounds_3857 = [b.left, b.bottom, b.right, b.top]
            else:
                global_bounds_3857[0] = min(global_bounds_3857[0], b.left)
                global_bounds_3857[1] = min(global_bounds_3857[1], b.bottom)
                global_bounds_3857[2] = max(global_bounds_3857[2], b.right)
                global_bounds_3857[3] = max(global_bounds_3857[3], b.top)

    west_m, south_m, east_m, north_m = global_bounds_3857
    west_lon, south_lat = merc_to_lonlat(west_m, south_m)
    east_lon, north_lat = merc_to_lonlat(east_m, north_m)
    center_lon = (west_lon + east_lon) / 2
    center_lat = (south_lat + north_lat) / 2
    print(f"[info] bounds WGS84: {west_lon:.4f},{south_lat:.4f},{east_lon:.4f},{north_lat:.4f}")

    # Abre SQLite
    conn = open_mbtiles(out_path)
    upsert_metadata(conn, {
        "name": args.name,
        "format": "webp",
        "type": "baselayer",
        "version": "1",
        "description": f"Mosaico {len(tifs)} imagens Sentinel-2 teste (6c EDGV)",
        "bounds": f"{west_lon:.6f},{south_lat:.6f},{east_lon:.6f},{north_lat:.6f}",
        "center": f"{center_lon:.6f},{center_lat:.6f},{args.zmin}",
        "minzoom": args.zmin,
        "maxzoom": args.zmax,
        "scheme": "xyz",
    })

    t_start = time.time()
    total_rendered = total_empty = total_skip = 0

    for z in range(args.zmin, args.zmax + 1):
        # Mapa global (x,y) -> lista de imagens que intersectam
        tile_sources: dict[tuple[int, int], list[Path]] = {}
        for p, (l, bt, r, t) in src_bounds:
            xmin, ymin, xmax, ymax = tiles_for_bbox(z, l, bt, r, t)
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    tile_sources.setdefault((x, y), []).append(p)

        candidates = list(tile_sources.items())
        n_cand = len(candidates)
        print(f"[z={z}] {n_cand:,} tiles candidatos, avg {np.mean([len(v) for v in tile_sources.values()]):.1f} fontes/tile")

        # Filtra já renderizados (resume) — 1 query por zoom em vez de N
        if args.resume:
            already = existing_xyz_for_zoom(conn, z)
            filt = [(xy, srcs) for xy, srcs in candidates if xy not in already]
            skipped = n_cand - len(filt)
            total_skip += skipped
            candidates = filt
            print(f"[z={z}] resume: {skipped:,} já renderizados, processando {len(candidates):,}")
            if not candidates:
                continue

        jobs = [([str(p) for p in srcs], z, x, y, args.quality) for (x, y), srcs in candidates]

        # Streaming em batches — submeter+consumir em janelas limitadas
        # limita RAM a ~= batch_size * avg_webp_size (~20-40 KB/tile)
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
                del futs  # libera Future objects
                done = start + len(batch)
                dt = time.time() - t_z
                rate = done / dt if dt > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  z={z}: {done}/{total}  ok={n_ok} empty={n_empty}  {rate:.0f} t/s  ETA {eta/60:.1f}m", flush=True)
        total_rendered += n_ok
        total_empty += n_empty
        print(f"[z={z}] ok={n_ok:,} empty={n_empty:,} em {(time.time()-t_z)/60:.1f} min")

    # Índice e finalização
    conn.execute("ANALYZE")
    # Funde o WAL no arquivo principal e desliga o modo WAL para não deixar
    # -wal/-shm órfãos (readers usam immutable=1, não precisam de WAL).
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    # Stats finais
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
