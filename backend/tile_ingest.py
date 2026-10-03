"""Tile ingestion service — single source of truth for turning (lat, lon)
centers into pending tiles using the project's geodesic geometry. Shared by the
admin UI endpoint (POST /api/admin/projects/{id}/tiles) and the import_points
CLI so both behave identically.

Raises ValueError(<code>) for bad input and LookupError("project_not_found")
when the project is missing; callers (router/CLI) translate to their own error
surface — same contract as export_service.
"""
from .database import transaction
from .geo import bbox_from_center, offset_center
from .mask_utils import empty_mask_png
from . import project_service
from .scripts._common import insert_tile_dedup

MAX_POINTS_PER_REQUEST = 5000
MAX_BLOCK = 25  # 25×25 = 625 tiles per point — guards against an accidental huge import


def add_points(project_id: int, points, block: int = 1) -> dict:
    """Insert pending tiles for each (lat, lon, name) center, optionally as an
    NxN adjacent block (odd `block`). Dedups per (project, bbox). Returns
    {inserted, skipped, tile_px, tile_meters}.

    `points` is an iterable of (lat, lon, name). `name` may be empty (a
    lat/lon-derived name is used)."""
    if not isinstance(block, int) or block < 1 or block % 2 == 0:
        raise ValueError("invalid_block")
    if block > MAX_BLOCK:
        raise ValueError("block_too_large")

    clean: list[tuple[float, float, str]] = []
    for i, p in enumerate(points):
        try:
            lat, lon, name = p[0], p[1], (p[2] if len(p) > 2 else None)
        except (IndexError, TypeError):
            raise ValueError("invalid_point_row")
        try:
            lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            raise ValueError("invalid_coordinate")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ValueError("coordinate_out_of_range")
        name = (str(name).strip() if name else "") or f"{lat:.4f}_{lon:.4f}"
        clean.append((lat, lon, name[:200]))

    if not clean:
        raise ValueError("no_points")
    if len(clean) > MAX_POINTS_PER_REQUEST:
        raise ValueError("too_many_points")

    proj = project_service.get_project(project_id)
    if not proj:
        raise LookupError("project_not_found")
    tile_px = int(proj.get("tile_px", 256))
    tile_meters = float(proj.get("tile_meters", tile_px * float(proj.get("meters_per_pixel", 2.5))))
    # Only raster tiles carry a data_png seed; classification leaves the
    # body NULL (read endpoints return an empty body for NULL).
    empty_png = empty_mask_png(tile_px) if proj.get("kind", "raster") == "raster" else None

    radius = block // 2
    inserted = skipped = 0
    with transaction("IMMEDIATE") as conn:
        for lat, lon, name in clean:
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    lat_c, lon_c = offset_center(lat, lon, dx, dy, tile_meters)
                    tname = name if block == 1 else f"{name}_{dx:+d}{dy:+d}"
                    bbox = bbox_from_center(lat_c, lon_c, tile_meters)
                    if insert_tile_dedup(conn, project_id, tname, bbox, empty_png):
                        inserted += 1
                    else:
                        skipped += 1
    return {"inserted": inserted, "skipped": skipped,
            "tile_px": tile_px, "tile_meters": tile_meters}
