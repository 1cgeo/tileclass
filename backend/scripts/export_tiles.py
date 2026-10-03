"""CLI: export GT tiles as georeferenced GeoTIFF in the canonical training format.

Canonical format (matches `treinamento_6c/<split>/<split>_masks/`):
    - single-band uint8 GeoTIFF
    - EPSG:4326, deflate compression
    - NODATA = 255
    - class IDs: EDGV 6c order (0..5) when remapped, else the project's own ids:
        0 = água              (TileClass id 1)
        1 = área edificada    (TileClass id 2)
        2 = terreno exposto   (TileClass id 6)
        3 = campo             (TileClass id 4)
        4 = floresta          (TileClass id 3)
        5 = cultivo           (TileClass id 5)

Remap policy (per project): `auto` (default) applies the EDGV remap only when
the project's class id set is exactly {1..6} (the legacy 6-class palette the
LUT was written for); any other palette is exported with its raw ids.
`--edgv` forces the remap, `--raw` forces raw ids. The manifest's `remap`
column records what was applied to each file.

Files are written as `gt_<tile_name>.tif` (`gt_p<project_id>_<tile_name>.tif`
on a multi-project export). Tile names are sanitized to [A-Za-z0-9._-] and
made unique within the export (`_t<tile_id>` suffix on collision); the
manifest `filename` column always points at the actual file.

Usage:
    python -m backend.scripts.export_tiles <out_dir> [options]

Options:
    --status reviewed              (default) only status='reviewed'
    --status reviewed+classified   include status='classified' too
    --raw                          force the project's own class ids (no remap)
    --edgv                         force the EDGV remap
    --mosaic                       also write a mosaic per project
                                   (gt_mosaic.tif with --project, else
                                   gt_mosaic_p<project_id>.tif per project)
    --manifest PATH                manifest CSV path (default: <out_dir>/manifest.csv)
"""
import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# PROJ fix (Windows ships conflicting proj.db installs) — must run before
# anything imports rasterio. Single implementation in backend/proj_env.py.
from backend.proj_env import configure_proj_data
configure_proj_data()

import numpy as np

from backend.database import connect
from backend.mask_utils import decode_mask
from backend import project_service
from backend.scripts._common import resolve_project_arg


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

# The only palette the LUT above is meaningful for.
EDGV_CLASS_IDS = frozenset(range(1, 7))

REMAP_MODES = ("auto", "edgv", "raw")


STATUS_FILTERS = {
    "reviewed": ("reviewed",),
    "classified": ("classified",),
    "reviewed+classified": ("reviewed", "classified"),
}

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_STEM = 120


def safe_name(name: str | None) -> str:
    """Tile name → filesystem-safe stem: only [A-Za-z0-9._-] (anything else,
    incl. path separators and ':' NTFS streams, becomes '_'), no leading dots
    (no hidden files / '..' traversal), bounded length, never empty."""
    s = _UNSAFE_CHARS.sub("_", name or "").lstrip(".")[:_MAX_STEM]
    return s or "tile"


def resolve_remap(remap: str, class_ids) -> str:
    """Effective remap ('edgv' | 'raw') for a project given the requested
    mode. `auto` → EDGV only for the legacy 6-class id set {1..6}."""
    if remap not in REMAP_MODES:
        raise ValueError(f"invalid remap: {remap!r} (expected one of {REMAP_MODES})")
    if remap == "auto":
        return "edgv" if set(class_ids) == EDGV_CLASS_IDS else "raw"
    return remap


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


def _select_rows(statuses: tuple[str, ...], project_id: int | None) -> list:
    """Raster tiles only. Joining `projects` filters out classification tiles
    (whose data_png is NULL) so a no --project run on a mixed-kind DB doesn't
    try to decode a non-raster body — matches the kind guard in
    export_classifications. Ordered by (project, name, id) so the filename
    de-duplication is deterministic (lowest id keeps the plain name)."""
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
                ORDER BY t.project_id, t.name, t.id""",
            params,
        ).fetchall()
    finally:
        conn.close()


def _decode_tile(row, remap: str, tile_px: int) -> np.ndarray:
    raw_bytes = decode_mask(row["data_png"], tile_px)
    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(tile_px, tile_px).copy()
    if remap == "edgv":
        arr = EDGV_REMAP_LUT[arr]
    return arr


def _write_mosaic(path: Path, tile_paths: list[Path]) -> Path:
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
    "remap",
]


def _manifest_row(fname: str, r, remap: str) -> list:
    return [
        fname, r["id"], r["project_id"], r["name"], r["status"],
        r["classified_by"], r["reviewed_by"],
        r["classified_at"], r["reviewed_at"],
        r["bbox_west"], r["bbox_south"], r["bbox_east"], r["bbox_north"],
        remap,
    ]


def _mosaic_name(pid: int, single_project: bool) -> str:
    return "gt_mosaic.tif" if single_project else f"gt_mosaic_p{pid}.tif"


def run(out_dir, *, status: str = "reviewed", project_id=None,
        remap: str = "auto", raw: bool = False, mosaic: bool = False,
        manifest_path=None) -> int:
    """Write per-tile GeoTIFF + manifest for raster `project_id` (None = all
    raster projects), filtered by `status` (a STATUS_FILTERS key). `remap` is
    'auto' | 'edgv' | 'raw' (see module doc); `raw=True` is the legacy alias of
    remap='raw'. `mosaic` writes one mosaic per project (projects differ in
    tile_px and class meaning, so they are never merged together). Returns the
    tile count. Shared by the CLI and the admin export endpoint."""
    if raw:
        remap = "raw"
    if remap not in REMAP_MODES:
        raise ValueError(f"invalid remap: {remap!r} (expected one of {REMAP_MODES})")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Path(manifest_path) if manifest_path else out_dir / "manifest.csv"
    rows = _select_rows(STATUS_FILTERS[status], project_id)
    single = project_id is not None
    # tile_px / remap vary per project; resolve once per project.
    proj_info: dict[int, tuple[int, str]] = {}

    def _info(pid: int) -> tuple[int, str]:
        if pid not in proj_info:
            proj = project_service.get_project(pid) or {}
            ids = [c["id"] for c in proj.get("classes") or []]
            proj_info[pid] = (int(proj.get("tile_px", 256)), resolve_remap(remap, ids))
        return proj_info[pid]

    # Lower-cased: Windows/macOS filesystems are case-insensitive. Mosaic names
    # are reserved up front so a tile literally named "mosaic" can't be
    # overwritten by the mosaic.
    used: set[str] = {manifest.name.lower()}
    if mosaic:
        used |= {_mosaic_name(pid, single).lower()
                 for pid in {r["project_id"] for r in rows}}

    def _unique(stem: str, tile_id: int) -> str:
        fname = f"gt_{stem}.tif"
        n = 0
        while fname.lower() in used:
            n += 1
            suffix = f"_t{tile_id}" + (f"_{n}" if n > 1 else "")
            fname = f"gt_{stem}{suffix}.tif"
        used.add(fname.lower())
        return fname

    paths_by_project: dict[int, list[Path]] = {}
    with manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        for r in rows:
            tile_px, eff_remap = _info(r["project_id"])
            arr = _decode_tile(r, eff_remap, tile_px)
            # On a multi-project export (no --project), prefix with the project
            # id so two projects with a same-named tile don't collide.
            prefix = "" if single else f"p{r['project_id']}_"
            fname = _unique(prefix + safe_name(r["name"]), r["id"])
            _write_geotiff(out_dir / fname, arr, (r["bbox_west"], r["bbox_south"],
                                                  r["bbox_east"], r["bbox_north"]), tile_px)
            paths_by_project.setdefault(r["project_id"], []).append(out_dir / fname)
            w.writerow(_manifest_row(fname, r, eff_remap))

    if mosaic:
        for pid, paths in paths_by_project.items():
            _write_mosaic(out_dir / _mosaic_name(pid, single), paths)
    return sum(len(p) for p in paths_by_project.values())


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("out_dir")
    p.add_argument("--status", choices=sorted(STATUS_FILTERS), default="reviewed")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--raw", action="store_true",
                   help="força os IDs originais do projeto (sem remap EDGV)")
    g.add_argument("--edgv", action="store_true",
                   help="força o remap EDGV (padrão: automático — só p/ paleta {1..6})")
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
    remap = "raw" if args.raw else "edgv" if args.edgv else "auto"
    n = run(args.out_dir, status=args.status, project_id=project_id,
            remap=remap, mosaic=args.mosaic, manifest_path=args.manifest)
    print(f"exported {n} tiles ({args.status}, remap={remap}) to {args.out_dir}")


if __name__ == "__main__":
    main()
