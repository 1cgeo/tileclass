"""Operator-facing tile endpoints: queue, resume, classify/review, problem,
pause/resume, history, image. The mask-body helpers are local since only
operator-side mutations carry a 65536-byte payload."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from .. import admin_service, auth, project_service, tile_service
from ..mask_utils import PIXELS
from ..models import ReportProblemIn, TileOut

router = APIRouter(prefix="/api", tags=["tiles"])


async def _read_mask_body(request: Request) -> bytes:
    """Validate Content-Length + read raw 65536-byte mask body."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            cl_int = int(cl)
        except ValueError:
            raise HTTPException(400, "invalid content-length")
        if cl_int > PIXELS:
            raise HTTPException(413, "mask body too large")
    raw = await request.body()
    if len(raw) != PIXELS:
        raise HTTPException(
            422,
            detail={"error": "invalid_mask_size", "expected": PIXELS, "got": len(raw)},
        )
    return raw


def _expected_version(request: Request) -> int | None:
    h = request.headers.get("x-tile-version")
    if not h:
        return None
    try:
        return int(h)
    except ValueError:
        raise HTTPException(400, "invalid X-Tile-Version header")


async def _submit(tile_id: int, request: Request, user: auth.CurrentUser) -> dict:
    raw = await _read_mask_body(request)
    return tile_service.submit_classification(tile_id, user.id, raw, _expected_version(request))


def _resolve_project_id(
    project_id: int | None,
    user: auth.CurrentUser,
    *,
    required: bool,
) -> int | None:
    """Resolve and authorise the project for queue endpoints. When `required`,
    a missing query param falls back to the user's single project membership
    (zero-friction default for single-project deployments). Membership check
    runs unconditionally so admins still hit the right project's tiles."""
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
    return project_id


@router.get("/tiles/next")
def next_tile(
    project_id: int | None = Query(default=None),
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    pid = _resolve_project_id(project_id, user, required=True)
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
    pid = _resolve_project_id(project_id, user, required=True)
    t = tile_service.peek_next_tile(user.id, pid)
    if not t:
        return Response(status_code=204)
    return t


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
    img = tile_service.get_tile_image(tile_id)
    if img is None:
        raise HTTPException(404, "tile not found")
    return Response(content=img, media_type="image/png")


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


@router.post("/tiles/{tile_id}/pause")
async def pause_tile(tile_id: int = Path(ge=1), *, request: Request,
                     user: auth.CurrentUser = Depends(auth.get_current_user)):
    raw = await _read_mask_body(request)
    return tile_service.pause_tile(tile_id, user.id, raw, _expected_version(request))


@router.post("/tiles/{tile_id}/resume")
def resume_tile(tile_id: int = Path(ge=1),
                user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.resume_tile(tile_id, user.id)
