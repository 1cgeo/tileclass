"""Operator-facing tile endpoints: queue, resume, classify/review, problem,
pause/resume, history, image. The mask-body helpers are local since only
operator-side mutations carry a tile_px²-byte payload."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from .. import admin_service, auth, project_service, tile_service
from ..models import ReportProblemIn, TileOut

router = APIRouter(prefix="/api", tags=["tiles"])


async def _read_mask_body(request: Request, expected_pixels: int) -> bytes:
    """Validate Content-Length + read a raw `expected_pixels`-byte mask body."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            cl_int = int(cl)
        except ValueError:
            raise HTTPException(400, "invalid content-length")
        if cl_int > expected_pixels:
            raise HTTPException(413, "mask body too large")
    raw = await request.body()
    if len(raw) != expected_pixels:
        raise HTTPException(
            422,
            detail={"error": "invalid_mask_size",
                    "expected": expected_pixels, "got": len(raw)},
        )
    return raw


async def _read_body_for_kind(request: Request, tile_id: int) -> tuple[bytes, dict]:
    """Pick the right body reader by the project's kind and return both the
    body and the resolved project dict so callers can hand `proj` to
    tile_service without a second project_for_tile lookup. Raster expects
    tile_px**2 raw bytes; vector accepts up to ~1.5 MB JSON; classification
    accepts a small JSON envelope (`{"class_id": int}`)."""
    proj = tile_service.project_for_tile(tile_id)
    kind = proj.get("kind")
    if kind in ("vector", "detection"):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > 1_500_000:
            raise HTTPException(413, "geojson body too large")
        return await request.body(), proj
    if kind == "classification":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > 1024:
            raise HTTPException(413, "classification body too large")
        return await request.body(), proj
    tile_px = int(proj.get("tile_px", 256))
    return await _read_mask_body(request, tile_px * tile_px), proj


def _expected_version(request: Request) -> int | None:
    h = request.headers.get("x-tile-version")
    if not h:
        return None
    try:
        return int(h)
    except ValueError:
        raise HTTPException(400, "invalid X-Tile-Version header")


async def _submit(tile_id: int, request: Request, user: auth.CurrentUser) -> dict:
    body, proj = await _read_body_for_kind(request, tile_id)
    return tile_service.submit_classification(
        tile_id, user.id, body, _expected_version(request), proj=proj,
    )


def _resolve_project_id(
    project_id: int | None,
    user: auth.CurrentUser,
    *,
    required: bool,
    enforce_active: bool = False,
) -> int | None:
    """Resolve and authorise the project for queue endpoints. When `required`,
    a missing query param falls back to the user's single project membership
    (zero-friction default for single-project deployments). Membership check
    runs unconditionally so admins still hit the right project's tiles.

    `enforce_active=True` adds a gate so /next-style endpoints refuse to
    serve from a soft-disabled project (admin set `active=False`). Read-only
    endpoints (assigned, queue-stats, stats-today) keep working so the
    operator can finish or report on tiles already assigned."""
    if project_id is None:
        if not required:
            return None
        memberships = project_service.list_projects_for_user(
            user.id, is_admin=user.role == "admin",
        )
        if len(memberships) == 1:
            return memberships[0]["id"]
        raise HTTPException(400, detail={"error": "project_id_required"})
    project_service.require_membership(project_id, user)
    if enforce_active:
        proj = project_service.get_project(project_id)
        if not proj or not proj.get("active"):
            raise HTTPException(409, detail={
                "error": "project_inactive",
                "message": "Projeto desativado: novas atribuições estão bloqueadas.",
            })
    return project_id


@router.get("/tiles/next")
def next_tile(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    pid = _resolve_project_id(project_id, user, required=True, enforce_active=True)
    t = tile_service.get_next_tile(user.id, pid)
    if not t:
        return Response(status_code=204)
    return t


@router.get("/tiles/assigned")
def my_assigned_tile(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    """Tile currently assigned (resume target), or 204. Never pulls from queue.
    `project_id` is optional — when omitted we look across every project the
    user is touching (useful right after login, before a project is picked)."""
    pid = _resolve_project_id(project_id, user, required=False)
    t = tile_service.get_resume_tile(user.id, pid)
    if not t:
        return Response(status_code=204)
    return t


@router.get("/tiles/next-preview")
def next_tile_preview(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    """Peek without assigning — used by the frontend to pre-load the next image."""
    pid = _resolve_project_id(project_id, user, required=True, enforce_active=True)
    t = tile_service.peek_next_tile(user.id, pid)
    if not t:
        return Response(status_code=204)
    return t


@router.get("/me/projects")
def my_projects_workload(user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Project picker payload for the operator UI: every project the user
    has access to, plus per-project counts of work available right now
    (pending + assigned-to-me + review queue). Frontend renders the count
    next to the project name and falls back to the next non-empty project
    when /tiles/next returns 204."""
    return tile_service.projects_with_user_workload(
        user.id, is_admin=user.role == "admin",
    )


@router.get("/me/stats-today")
def my_stats_today(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    pid = _resolve_project_id(project_id, user, required=False)
    return {"count": tile_service.today_classify_count(user.id, pid)}


@router.get("/tiles/queue-stats")
def queue_stats(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    pid = _resolve_project_id(project_id, user, required=False)
    return tile_service.queue_stats(pid)


@router.get("/tiles/{tile_id}/history")
def tile_history(tile_id: int = Path(ge=1),
                 _: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.tile_history(tile_id)


@router.get("/tiles/{tile_id}/review-note")
def tile_review_note(tile_id: int = Path(ge=1),
                     _: auth.CurrentUser = Depends(auth.get_current_user)):
    """Most recent `request_changes` note for this tile, or 204 if none.
    The editor surfaces this as a banner when the classifier reopens the tile."""
    note = tile_service.latest_review_note(tile_id)
    if not note:
        return Response(status_code=204)
    return note


@router.get("/tiles/{tile_id}", response_model=TileOut)
def get_tile(tile_id: int = Path(ge=1),
             user: auth.CurrentUser = Depends(auth.get_current_user)):
    t = tile_service.get_tile(tile_id)
    if not t:
        raise HTTPException(404, "tile not found")
    return t


@router.get("/tiles/{tile_id}/image")
def get_tile_image(tile_id: int = Path(ge=1),
                   user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Raster body only. Vector/classification tiles use /features and
    /classification respectively; calling /image on them returns 415."""
    proj = tile_service.project_for_tile(tile_id)
    if proj.get("kind") != "raster":
        raise HTTPException(415, detail={
            "error": "wrong_kind",
            "kind": proj.get("kind"),
            "message": "Endpoint /image só está disponível para tiles raster.",
        })
    img = tile_service.get_tile_image(tile_id)
    if img is None:
        raise HTTPException(404, "tile not found")
    return Response(content=img, media_type="image/png")


@router.get("/tiles/{tile_id}/features")
def get_tile_features(tile_id: int = Path(ge=1),
                      user: auth.CurrentUser = Depends(auth.get_current_user)):
    """GeoJSON body — returns the FeatureCollection JSON (LineStrings for
    vector, box Polygons for detection), or the canonical empty FC if the tile
    has never been submitted."""
    proj = tile_service.project_for_tile(tile_id)
    if proj.get("kind") not in ("vector", "detection"):
        raise HTTPException(415, detail={
            "error": "wrong_kind",
            "kind": proj.get("kind"),
            "message": "Endpoint /features só está disponível para tiles vetoriais/detection.",
        })
    text = tile_service.get_tile_geojson(tile_id)
    if text is None:
        raise HTTPException(404, "tile not found")
    return Response(content=text, media_type="application/json")


@router.get("/tiles/{tile_id}/classification")
def get_tile_classification(tile_id: int = Path(ge=1),
                            user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Classification body — returns `{"class_id": int}`, or 204 when the tile
    has not been classified yet."""
    proj = tile_service.project_for_tile(tile_id)
    if proj.get("kind") != "classification":
        raise HTTPException(415, detail={
            "error": "wrong_kind",
            "kind": proj.get("kind"),
            "message": "Endpoint /classification só está disponível para tiles classification.",
        })
    cid = tile_service.get_tile_class_id(tile_id)
    if cid is None:
        return Response(status_code=204)
    return {"class_id": cid}


@router.get("/tiles/{tile_id}/satellite-thumbnail")
def get_tile_satellite_thumbnail(tile_id: int = Path(ge=1),
                                 size: int = Query(256, ge=16, le=512),
                                 _: auth.CurrentUser = Depends(auth.get_current_user)):
    return Response(
        content=admin_service.tile_satellite_thumbnail(tile_id, size=size),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/tiles/{tile_id}/classify")
async def classify(tile_id: int = Path(ge=1), *, request: Request,
                   user: auth.CurrentUser = Depends(auth.get_current_user)):
    return await _submit(tile_id, request, user)


@router.post("/tiles/{tile_id}/review")
async def review(tile_id: int = Path(ge=1), *, request: Request,
                 user: auth.CurrentUser = Depends(auth.get_current_user)):
    return await _submit(tile_id, request, user)


@router.post("/tiles/{tile_id}/report-problem")
def report_problem(body: ReportProblemIn, tile_id: int = Path(ge=1),
                   user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.report_problem(tile_id, user.id, body.note)


@router.post("/tiles/{tile_id}/request-changes")
def request_changes(body: ReportProblemIn, tile_id: int = Path(ge=1),
                    user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Reviewer-only kick-back: tile returns to pending with a note attached
    for the next classifier (typically the original one). Mask is preserved."""
    return tile_service.request_changes(tile_id, user.id, body.note)


@router.post("/tiles/{tile_id}/pause")
async def pause_tile(tile_id: int = Path(ge=1), *, request: Request,
                     user: auth.CurrentUser = Depends(auth.get_current_user)):
    body, proj = await _read_body_for_kind(request, tile_id)
    return tile_service.pause_tile(
        tile_id, user.id, body, _expected_version(request), proj=proj,
    )


@router.post("/tiles/{tile_id}/resume")
def resume_tile(tile_id: int = Path(ge=1),
                user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.resume_tile(tile_id, user.id)


@router.post("/tiles/{tile_id}/heartbeat")
def heartbeat(tile_id: int = Path(ge=1),
              user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Editor pings while a tile is open so the auto-pause sweep can tell
    apart "operator working" vs "operator dropped off"."""
    return tile_service.heartbeat(tile_id, user.id)
