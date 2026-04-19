"""Gera pirâmide XYZ (PNG) a partir de um GeoTIFF em EPSG:3857.

Saída: <out_dir>/<z>/<x>/<y>.png
Esquema XYZ (origem no canto noroeste, y crescendo p/ sul) — padrão MapLibre.

Uso:
  python build_xyz_pyramid.py <input.tif> <out_dir> --zmin 10 --zmax 17
"""
from __future__ import annotations
import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import reproject

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tile_grid import TILE, tile_bounds_3857, tiles_for_bbox

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def render_tile(args) -> tuple[int, str]:
    src_path, out_dir, z, x, y, bands = args
    out_file = Path(out_dir) / str(z) / str(x) / f"{y}.png"
    if out_file.exists():
        return 0, "skip"
    w, s, e, n = tile_bounds_3857(z, x, y)
    res = (e - w) / TILE
    dst_transform = rasterio.transform.from_origin(w, n, res, res)

    with rasterio.open(src_path) as src:
        # Verifica se a tile intersecta o raster
        sb = src.bounds
        if e <= sb.left or w >= sb.right or n <= sb.bottom or s >= sb.top:
            return 0, "out"

        dst = np.zeros((len(bands), TILE, TILE), dtype=np.uint8)
        for i, b in enumerate(bands):
            reproject(
                source=rasterio.band(src, b),
                destination=dst[i],
                src_crs="EPSG:3857",
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.bilinear,
            )

    if dst.sum() == 0:
        return 0, "empty"

    arr = np.transpose(dst, (1, 2, 0))  # H,W,C
    alpha = (arr.sum(axis=2) > 0).astype(np.uint8) * 255
    rgba = np.dstack([arr, alpha])
    out_file.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(out_file, "PNG", optimize=False)
    return 1, "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("out_dir")
    ap.add_argument("--zmin", type=int, default=10)
    ap.add_argument("--zmax", type=int, default=17)
    ap.add_argument("--bands", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    src_path = str(Path(args.input).resolve())
    out_dir = str(Path(args.out_dir).resolve())
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    with rasterio.open(src_path) as src:
        epsg = src.crs.to_epsg() if src.crs else None
        wkt = src.crs.to_wkt() if src.crs else ""
        if epsg != 3857 and "Pseudo-Mercator" not in wkt:
            raise SystemExit(f"esperado Web Mercator (EPSG:3857), obtido {src.crs}")
        sb = src.bounds
        print(f"raster: {src.width}x{src.height}  bounds={sb}")

    total_tiles = 0
    for z in range(args.zmin, args.zmax + 1):
        xmin, ymin, xmax, ymax = tiles_for_bbox(z, sb.left, sb.bottom, sb.right, sb.top)
        n_tiles = (xmax - xmin + 1) * (ymax - ymin + 1)
        print(f"z={z}: x={xmin}..{xmax} y={ymin}..{ymax}  {n_tiles:,} tiles candidatos")
        total_tiles += n_tiles

        jobs = [(src_path, out_dir, z, x, y, args.bands)
                for x in range(xmin, xmax + 1) for y in range(ymin, ymax + 1)]
        ok = empty = out = skip = 0
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(render_tile, j) for j in jobs]
            for i, f in enumerate(as_completed(futs), 1):
                _, status = f.result()
                if status == "ok": ok += 1
                elif status == "empty": empty += 1
                elif status == "out": out += 1
                elif status == "skip": skip += 1
                if i % 500 == 0:
                    print(f"  {i}/{n_tiles}  ok={ok} empty={empty} out={out} skip={skip}", end="\r")
        print(f"  z={z}: ok={ok} empty={empty} out={out} skip={skip}")

    print(f"\n[OK] total candidatos: {total_tiles:,}")


if __name__ == "__main__":
    main()
