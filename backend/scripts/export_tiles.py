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
from backend.mask_utils import decode_mask
from backend import project_service


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
    "classified": ("classified",),
    "reviewed+classified": ("reviewed", "classified"),
}


def _write_geotiff(out: Path, arr: np.ndarray, bbox: tuple[float, float, float, float],
                   tile_px: int) -> None:
    import rasterio
    from rasterio.transform import from_bounds

    west, south, east, north = bbox
    transform = from_bounds(west, south, east, north, tile_px, tile_px)
    with rasterio.open(
        out, "w",
        driver="GTiff",
        height=tile_px, width=tile_px,
        count=1, dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=255,
        compress="deflate",
    ) as dst:
        dst.write(arr, 1)


from backend.scripts._common import resolve_project_arg


def _select_rows(statuses: tuple[str, ...], project_id: int | None) -> list:
    """Raster tiles only. Joining `projects` filters out vector/detection/
    classification tiles (whose data_png is NULL) so a no --project run on a
    mixed-kind DB doesn't try to decode a non-raster body — matches the kind
    guard the other three exporters use."""
    placeholders = ",".join("?" for _ in statuses)
    extra = "" if project_id is None else " AND t.project_id=?"
    params = list(statuses) + ([] if project_id is None else [project_id])
    conn = connect()
    try:
        return conn.execute(
            f"""SELECT t.id, t.project_id, t.name, t.status,
                       t.classified_by, t.reviewed_by,
                       t.classified_at, t.reviewed_at,
                       t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north,
                       t.data_png
                FROM tiles t JOIN projects p ON p.id=t.project_id
                WHERE p.kind='raster' AND t.status IN ({placeholders})
                  AND t.data_png IS NOT NULL{extra}
                ORDER BY t.name""",
            params,
        ).fetchall()
    finally:
        conn.close()


def _decode_tile(row, raw: bool, tile_px: int) -> np.ndarray:
    raw_bytes = decode_mask(row["data_png"], tile_px)
    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(tile_px, tile_px).copy()
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


def run(out_dir, *, status: str = "reviewed", project_id=None,
        raw: bool = False, mosaic: bool = False, manifest_path=None) -> int:
    """Write per-tile GeoTIFF + manifest for raster `project_id` (None = all
    raster projects), filtered by `status` (a STATUS_FILTERS key). `raw` keeps
    native TileClass IDs (skips EDGV remap). Returns the tile count. Shared by
    the CLI and the admin export endpoint."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Path(manifest_path) if manifest_path else out_dir / "manifest.csv"
    rows = _select_rows(STATUS_FILTERS[status], project_id)
    # tile_px varies per project; cache lookups so we hit the DB once per project.
    px_by_project: dict[int, int] = {}

    def _px_for(pid: int) -> int:
        if pid not in px_by_project:
            proj = project_service.get_project(pid) or {}
            px_by_project[pid] = int(proj.get("tile_px", 256))
        return px_by_project[pid]

    paths: list[Path] = []
    with manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        for r in rows:
            tile_px = _px_for(r["project_id"])
            arr = _decode_tile(r, raw, tile_px)
            # On a multi-project export (no --project), prefix with the project
            # id so two projects with a same-named tile don't overwrite each
            # other (the manifest still distinguishes them by project_id).
            prefix = "" if project_id is not None else f"p{r['project_id']}_"
            fname = f"gt_{prefix}{r['name']}.tif"
            _write_geotiff(out_dir / fname, arr, (r["bbox_west"], r["bbox_south"],
                                                  r["bbox_east"], r["bbox_north"]), tile_px)
            paths.append(out_dir / fname)
            w.writerow(_manifest_row(fname, r))

    if mosaic and paths:
        _write_mosaic(out_dir, paths)
    return len(paths)


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

    conn = connect()
    try:
        project_id = resolve_project_arg(conn, args.project, allow_all=True)
    finally:
        conn.close()
    n = run(args.out_dir, status=args.status, project_id=project_id,
            raw=args.raw, mosaic=args.mosaic, manifest_path=args.manifest)
    fmt = "raw 1..6" if args.raw else "EDGV 0..5"
    print(f"exported {n} tiles ({args.status}, {fmt}) to {args.out_dir}")


if __name__ == "__main__":
    main()
