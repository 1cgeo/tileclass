"""CLI: export reviewed tiles as georeferenced GeoTIFF (single band)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import connect
from backend.mask_utils import decode_mask, TILE_SIZE

import numpy as np


def _write_geotiff(out: Path, arr: np.ndarray, bbox: tuple[float, float, float, float]):
    import rasterio
    from rasterio.transform import from_bounds

    west, south, east, north = bbox
    transform = from_bounds(west, south, east, north, TILE_SIZE, TILE_SIZE)
    with rasterio.open(
        out, "w",
        driver="GTiff",
        height=TILE_SIZE, width=TILE_SIZE,
        count=1, dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        compress="lzw",
    ) as dst:
        dst.write(arr, 1)


def main():
    if len(sys.argv) < 2:
        print("usage: python -m backend.scripts.export_tiles <out_dir> [--mosaic]")
        sys.exit(1)
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    do_mosaic = "--mosaic" in sys.argv

    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, name, bbox_west, bbox_south, bbox_east, bbox_north, data_png
               FROM tiles WHERE status='reviewed'"""
        ).fetchall()
    finally:
        conn.close()

    paths: list[Path] = []
    for r in rows:
        raw = decode_mask(r["data_png"])
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE).copy()
        out = out_dir / f"{r['name']}.tif"
        _write_geotiff(out, arr, (r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"]))
        paths.append(out)
    print(f"exported {len(paths)} tiles to {out_dir}")

    if do_mosaic and paths:
        import rasterio
        from rasterio.merge import merge
        srcs = [rasterio.open(p) for p in paths]
        mosaic, transform = merge(srcs)
        meta = srcs[0].meta.copy()
        meta.update({"height": mosaic.shape[1], "width": mosaic.shape[2], "transform": transform, "compress": "lzw"})
        with rasterio.open(out_dir / "mosaic.tif", "w", **meta) as dst:
            dst.write(mosaic)
        for s in srcs:
            s.close()
        print(f"mosaic written to {out_dir / 'mosaic.tif'}")


if __name__ == "__main__":
    main()
