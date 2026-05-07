"""CLI: export GT tiles as georeferenced GeoTIFF in the canonical training format.

Canonical format (matches `treinamento_6c/<split>/<split>_masks/`):
    - single-band uint8 GeoTIFF
    - EPSG:4326, deflate compression
    - NODATA = 255
    - class IDs in EDGV 6c order (0..5):
        0 = água              (TileClass id 1)
        1 = área edificada    (TileClass id 2)
        2 = terreno exposto   (TileClass id 6)
        3 = campo             (TileClass id 4)
        4 = floresta          (TileClass id 3)
        5 = cultivo           (TileClass id 5)

Files are written as `gt_<tile_name>.tif` plus a `manifest.csv` summary.

Usage:
    python -m backend.scripts.export_tiles <out_dir> [options]

Options:
    --status reviewed              (default) only status='reviewed'
    --status reviewed+classified   include status='classified' too
    --raw                          keep TileClass IDs 1..6 (skip EDGV remap)
    --mosaic                       also write gt_mosaic.tif
    --manifest PATH                manifest CSV path (default: <out_dir>/manifest.csv)
"""
import argparse
import csv
import os
import sys
from pathlib import Path

# PROJ fix (Windows tem 3 proj.db conflitantes — PostgreSQL/PostGIS, pyproj,
# rasterio). Aponta PROJ_DATA para o diretório do rasterio antes de importar
# qualquer coisa que toque CRS.
_PY_SP = Path(sys.executable).parent / "Lib" / "site-packages"
os.environ.setdefault("PROJ_DATA", str(_PY_SP / "rasterio" / "proj_data"))

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.database import connect
from backend.mask_utils import decode_mask, TILE_SIZE


# TileClass class IDs (1..6) → EDGV training IDs (0..5). 255 (NODATA) and
# unused indexes pass through as identity. Source: backend/config.yaml ×
# treinamento_6c/CLAUDE.md classes.
EDGV_REMAP_LUT = np.arange(256, dtype=np.uint8)
EDGV_REMAP_LUT[1] = 0   # água        → agua
EDGV_REMAP_LUT[2] = 1   # edificada   → edif
EDGV_REMAP_LUT[3] = 4   # floresta    → floresta
EDGV_REMAP_LUT[4] = 3   # campo       → campo
EDGV_REMAP_LUT[5] = 5   # cultivo     → veg_cultivada
EDGV_REMAP_LUT[6] = 2   # terreno exp → terr_exp


STATUS_FILTERS = {
    "reviewed": ("reviewed",),
    "reviewed+classified": ("reviewed", "classified"),
}


def _write_geotiff(out: Path, arr: np.ndarray, bbox: tuple[float, float, float, float]) -> None:
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
        nodata=255,
        compress="deflate",
    ) as dst:
        dst.write(arr, 1)


def _resolve_project(project_arg: str | None) -> int | None:
    """Resolve --project to a project_id. None = export all projects."""
    if project_arg is None:
        return None
    conn = connect()
    try:
        rows = conn.execute("SELECT id, name FROM projects").fetchall()
    finally:
        conn.close()
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


def _select_rows(statuses: tuple[str, ...], project_id: int | None) -> list:
    placeholders = ",".join("?" for _ in statuses)
    conn = connect()
    try:
        if project_id is None:
            return conn.execute(
                f"""SELECT id, project_id, name, status,
                           classified_by, reviewed_by,
                           classified_at, reviewed_at,
                           bbox_west, bbox_south, bbox_east, bbox_north,
                           data_png
                    FROM tiles
                    WHERE status IN ({placeholders})
                    ORDER BY name""",
                statuses,
            ).fetchall()
        return conn.execute(
            f"""SELECT id, project_id, name, status,
                       classified_by, reviewed_by,
                       classified_at, reviewed_at,
                       bbox_west, bbox_south, bbox_east, bbox_north,
                       data_png
                FROM tiles
                WHERE status IN ({placeholders}) AND project_id=?
                ORDER BY name""",
            (*statuses, project_id),
        ).fetchall()
    finally:
        conn.close()


def _decode_tile(row, raw: bool) -> np.ndarray:
    raw_bytes = decode_mask(row["data_png"])
    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE).copy()
    if not raw:
        arr = EDGV_REMAP_LUT[arr]
    return arr


def _write_mosaic(out_dir: Path, tile_paths: list[Path]) -> Path:
    import rasterio
    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, transform = merge(srcs)
        meta = srcs[0].meta.copy()
        meta.update({
            "height": mosaic.shape[1], "width": mosaic.shape[2],
            "transform": transform, "compress": "deflate", "nodata": 255,
        })
        path = out_dir / "gt_mosaic.tif"
        with rasterio.open(path, "w", **meta) as dst:
            dst.write(mosaic)
        return path
    finally:
        for s in srcs:
            s.close()


MANIFEST_HEADER = [
    "filename", "tile_id", "project_id", "name", "status",
    "classified_by", "reviewed_by",
    "classified_at", "reviewed_at",
    "bbox_west", "bbox_south", "bbox_east", "bbox_north",
]


def _manifest_row(fname: str, r) -> list:
    return [
        fname, r["id"], r["project_id"], r["name"], r["status"],
        r["classified_by"], r["reviewed_by"],
        r["classified_at"], r["reviewed_at"],
        r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"],
    ]


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("out_dir")
    p.add_argument("--status", choices=sorted(STATUS_FILTERS), default="reviewed")
    p.add_argument("--raw", action="store_true",
                   help="keep TileClass IDs 1..6 (skip EDGV remap)")
    p.add_argument("--mosaic", action="store_true")
    p.add_argument("--manifest", default=None,
                   help="manifest CSV path (default: <out_dir>/manifest.csv)")
    p.add_argument("--project", type=str, default=None,
                   help="id ou nome do projeto a exportar. Omitir = todos.")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else out_dir / "manifest.csv"
    statuses = STATUS_FILTERS[args.status]
    project_id = _resolve_project(args.project)

    rows = _select_rows(statuses, project_id)
    paths: list[Path] = []
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        for r in rows:
            arr = _decode_tile(r, args.raw)
            fname = f"gt_{r['name']}.tif"
            out = out_dir / fname
            _write_geotiff(out, arr, (r["bbox_west"], r["bbox_south"],
                                      r["bbox_east"], r["bbox_north"]))
            paths.append(out)
            w.writerow(_manifest_row(fname, r))

    fmt = "raw 1..6" if args.raw else "EDGV 0..5"
    print(f"exported {len(paths)} tiles ({args.status}, {fmt}) to {out_dir}")
    print(f"manifest: {manifest_path}")

    if args.mosaic and paths:
        mpath = _write_mosaic(out_dir, paths)
        print(f"mosaic: {mpath}")


if __name__ == "__main__":
    main()
