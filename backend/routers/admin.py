"""Admin-only endpoints: dashboard, tile/user mutations, mask overlay tiles."""
from datetime import datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse

from .. import auth, admin_service, mask_tile_service, mbtiles_service, project_service
from ..models import (
    AssignTileIn, BulkAssignIn, BulkReportProblemIn, BulkTileIdsIn,
    CreateUserIn, DashboardOut, ResetReasonIn, SetActiveIn, SetCanReviewIn,
    SetRoleIn,
)

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(auth.require_admin)])


# Path-validated subset of TileStatus mirroring backend/database.py schema.
class TileStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    classified = "classified"
    in_review = "in_review"
    reviewed = "reviewed"
    problem = "problem"
    blocked = "blocked"


def _parse_iso_date(value: str, field: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"invalid {field}: expected YYYY-MM-DD")
    return value


@router.get("/dashboard", response_model=DashboardOut)
def admin_dashboard(project_id: int | None = Query(default=None, ge=1)):
    return admin_service.dashboard(project_id=project_id)


@router.get("/class-distribution")
def admin_class_distribution(project_id: int | None = Query(default=None, ge=1)):
    """Per-class pixel totals across classified/reviewed tiles, ready to
    feed a stacked bar in the dashboard. Tiles with no class_counts cache
    are skipped — run scripts/recompute_class_counts.py to backfill."""
    return admin_service.class_distribution(project_id=project_id)


@router.get("/feature-distribution")
def admin_feature_distribution(project_id: int | None = Query(default=None, ge=1)):
    """Vector counterpart of class-distribution: occurrences per
    (attribute_key, value) for enum/boolean attributes. The dashboard
    renders one section per attribute key, with bars per value."""
    return admin_service.feature_distribution(project_id=project_id)


_TILE_SORT_KEYS = {
    "id", "name", "status",
    "classified_by_username", "reviewed_by_username",
    "classified_at", "reviewed_at",
}


@router.get("/tiles")
def admin_tiles(status: TileStatus | None = None,
                user_id: int | None = Query(default=None, ge=1),
                date_from: str | None = None, date_to: str | None = None,
                paused: bool | None = None,
                q: str | None = Query(default=None, max_length=200),
                sort_by: str | None = Query(default=None, max_length=64),
                sort_dir: str | None = Query(default=None, pattern="^(asc|desc)$"),
                limit: int = Query(default=200, ge=1, le=1000),
                offset: int = Query(default=0, ge=0),
                project_id: int | None = Query(default=None, ge=1)):
    if date_from:
        date_from = _parse_iso_date(date_from, "date_from")
    if date_to:
        date_to = _parse_iso_date(date_to, "date_to")
    if sort_by is not None and sort_by not in _TILE_SORT_KEYS:
        raise HTTPException(422, f"invalid sort_by: {sort_by}")
    status_v = status.value if status else None
    q_norm = q.strip() if q else None
    items = admin_service.list_tiles(
        status=status_v, user_id=user_id, date_from=date_from, date_to=date_to,
        paused=paused, q=q_norm, sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset, project_id=project_id,
    )
    total = admin_service.count_tiles(
        status=status_v, user_id=user_id, date_from=date_from, date_to=date_to,
        paused=paused, q=q_norm, project_id=project_id,
    )
    # Pagination metadata in headers keeps the JSON body a plain list so
    # existing clients/tests that index into it keep working.
    return JSONResponse(content=items, headers={"X-Total-Count": str(total)})


@router.get("/tiles/{tile_id}/thumbnail")
def admin_tile_thumbnail(tile_id: int = Path(ge=1),
                         size: int = Query(128, ge=16, le=512)):
    return Response(
        content=admin_service.tile_thumbnail(tile_id, size=size),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/tiles/{tile_id}/satellite-thumbnail")
def admin_tile_satellite_thumbnail(tile_id: int = Path(ge=1),
                                   size: int = Query(128, ge=16, le=512)):
    return Response(
        content=admin_service.tile_satellite_thumbnail(tile_id, size=size),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/tiles/problems")
def admin_problems(project_id: int | None = Query(default=None, ge=1)):
    return admin_service.list_problems(project_id=project_id)


@router.get("/tiles/map")
def admin_tiles_map(project_id: int | None = Query(default=None, ge=1)):
    return admin_service.list_tiles_map(project_id=project_id)


@router.get("/mask-tiles/{project_id}/{z}/{x}/{y}.png")
def admin_mask_tile(
    project_id: int = Path(ge=1),
    z: int = Path(ge=0, le=22),
    x: int = Path(ge=0),
    y: int = Path(ge=0),
):
    """Colorized mask overlay for the admin map view, scoped to one project
    so palettes/tilesets never bleed between projects."""
    if z < mask_tile_service.min_zoom() or z > mask_tile_service.max_zoom():
        raise HTTPException(404, "zoom out of range")
    n = 1 << z
    if x >= n or y >= n:
        raise HTTPException(404, "tile out of range")
    png = mask_tile_service.get_tile(project_id, z, x, y)
    return Response(
        content=png,
        media_type="image/png",
        # Server caches indefinitely (invalidated on mutations); client caches
        # briefly so panning around stays snappy without holding stale colors
        # after a classify/review.
        headers={"Cache-Control": "public, max-age=60"},
    )


# ---------- Bulk tile actions ----------
# Bulk routes registered before /{tile_id}/... so FastAPI matches the literal
# "bulk" path segment instead of trying to coerce it into the int Path param.

@router.post("/tiles/bulk/reset")
def admin_bulk_reset(body: BulkTileIdsIn,
                     u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.reset_many(body.ids, u.id, body.reason)
    return {"affected": n}


@router.post("/tiles/bulk/re-review")
def admin_bulk_rereview(body: BulkTileIdsIn,
                        u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.re_review_many(body.ids, u.id, reason=body.reason)
    return {"affected": n}


@router.post("/tiles/bulk/report-problem")
def admin_bulk_report_problem(body: BulkReportProblemIn,
                              u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.report_problem_many(body.ids, u.id, body.note)


@router.post("/tiles/bulk/unassign")
def admin_bulk_unassign(body: BulkTileIdsIn,
                        u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.unassign_many(body.ids, u.id, body.reason)
    return {"affected": n}


@router.post("/tiles/bulk/block")
def admin_bulk_block(body: BulkTileIdsIn,
                     u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.block_many(body.ids, u.id, body.reason)
    return {"affected": n}


@router.post("/tiles/bulk/unblock")
def admin_bulk_unblock(body: BulkTileIdsIn,
                       u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.unblock_many(body.ids, u.id, body.reason)
    return {"affected": n}


@router.post("/tiles/assign")
def admin_bulk_assign(body: BulkAssignIn,
                      u: auth.CurrentUser = Depends(auth.require_admin)):
    """Pre-load a user's personal queue with many tiles. Every assigned tile
    enters `paused_at=now()`, so /api/tiles/next serves them FIFO when the user
    asks for more work."""
    return admin_service.assign_many(body.tile_ids, body.user_id, u.id, body.reason)


# ---------- Per-tile actions ----------

@router.post("/tiles/{tile_id}/reset")
def admin_reset(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.reset_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@router.post("/tiles/{tile_id}/assign")
def admin_assign(body: AssignTileIn, tile_id: int = Path(ge=1),
                 u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.assign_operator(tile_id, body.user_id, u.id, body.reason)


@router.post("/tiles/{tile_id}/unassign")
def admin_unassign(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                   u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.unassign_operator(tile_id, u.id, body.reason if body else None)


@router.post("/tiles/{tile_id}/admin-pause")
def admin_pause(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                u: auth.CurrentUser = Depends(auth.require_admin)):
    """Pause an in-progress/in-review tile on behalf of an absent operator so
    the dashboard cycle timer stops. Keeps the assignment intact."""
    return admin_service.admin_pause_tile(tile_id, u.id, body.reason if body else None)


@router.delete("/tiles/{tile_id}")
def admin_delete_tile(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                      u: auth.CurrentUser = Depends(auth.require_admin)):
    """Permanent delete. Only allowed for tiles already flagged as `problem`
    so an operator's report always precedes the admin's removal."""
    return admin_service.delete_tile(tile_id, u.id, body.reason if body else None)


@router.post("/tiles/{tile_id}/re-review")
def admin_re_review(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                    u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.re_review_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@router.post("/tiles/{tile_id}/block")
def admin_block(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.block_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@router.post("/tiles/{tile_id}/unblock")
def admin_unblock(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                  u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.unblock_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


# ---------- Users ----------

@router.get("/users")
def admin_users():
    return admin_service.list_users()


@router.post("/users")
def admin_create_user(body: CreateUserIn):
    return admin_service.create_user(body.username, body.password, body.role)


@router.patch("/users/{user_id}/active")
def admin_set_user_active(body: SetActiveIn, user_id: int = Path(ge=1),
                          u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_active(user_id, body.active, u.id)


@router.patch("/users/{user_id}/can-review")
def admin_set_user_can_review(body: SetCanReviewIn, user_id: int = Path(ge=1),
                              u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_can_review(user_id, body.can_review, u.id)


@router.patch("/users/{user_id}/role")
def admin_set_user_role(body: SetRoleIn, user_id: int = Path(ge=1),
                        u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_role(user_id, body.role, u.id)


# ---------- Maintenance ----------

def _layer_info(project_id: int, layer: str) -> dict:
    """Per-layer status for the maintenance overview: not-configured,
    remote (Martin / TileServer-GL URL), configured-but-broken, or open
    with reader metadata."""
    proj = project_service.get_project(project_id)
    if not proj:
        return {"open": False, "configured": False}
    src = project_service.layer_path(proj, layer)
    if not src:
        return {"open": False, "configured": False}
    if project_service.is_remote_layer(src):
        return {"open": True, "configured": True, "remote": True, "path": src}
    reader = mbtiles_service.get_reader(project_id, layer)
    if reader is None or not reader.is_open():
        return {"open": False, "configured": True, "path": src}
    lo, hi = reader.zoom_range()
    p = reader.path()
    return {
        "open": True,
        "configured": True,
        "format": reader.tile_format(),
        "min_zoom": lo,
        "max_zoom": hi,
        "path": str(p) if p else None,
    }


@router.get("/maintenance/overview")
def admin_maintenance_overview():
    """State of mbtiles readers (per project × layer) + size/contents of the
    on-disk overlay cache. Powers the admin Manutenção tab; safe to poll."""
    from ..database import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE active=1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    projects_layers = {
        r["id"]: {
            "name": r["name"],
            "layers": {layer: _layer_info(r["id"], layer)
                       for layer in project_service.LAYER_KEYS},
        }
        for r in rows
    }
    return {
        "projects": projects_layers,
        "overlay_cache": mask_tile_service.cache_stats(),
    }


@router.post("/maintenance/overlay-cache/clear")
def admin_clear_overlay_cache(u: auth.CurrentUser = Depends(auth.require_admin)):
    """Wipe every cached overlay tile. Renderings restart on the next admin
    map view; mutation hooks keep invalidation correct so this is rarely
    needed (use after a bulk import or palette change)."""
    deleted = mask_tile_service.clear_cache()
    return {"deleted": deleted}
