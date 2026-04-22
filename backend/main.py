"""FastAPI app: auth, tiles, admin, config, static frontend."""
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path as FsPath
import jwt as _jwt
from fastapi import FastAPI, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware

from . import auth, tile_service, admin_service, mbtiles_service
from .config import get_config
from .database import init_db
from .mask_utils import PIXELS
from .models import (
    LoginIn, TokenOut, RefreshIn, UserOut, ClassOut, TileOut,
    ReportProblemIn, CreateUserIn, DashboardOut, BulkTileIdsIn, SetActiveIn,
    SetCanReviewIn, AssignTileIn, BulkAssignIn, ResetReasonIn,
)


# Anything not listed here falls back to the raw message.
_FRIENDLY_ERRORS = {
    "invalid_mask": "Máscara inválida. Tente recarregar o tile.",
    "invalid_mask_size": "Tamanho de máscara inválido — recarregue a página.",
    "unfilled_pixels": "Ainda faltam pixels para preencher.",
    "tile_modified": "Este tile foi modificado por um administrador. Seu progresso foi salvo localmente — recarregue para continuar.",
    "mask_too_large": "Máscara muito grande para salvar. Contate o admin.",
    "too many login attempts": "Muitas tentativas. Aguarde 1 minuto.",
    "invalid credentials": "Usuário ou senha incorretos.",
    "missing token": "Sessão expirada. Faça login novamente.",
    "token revoked": "Sessão revogada. Faça login novamente.",
    "not assigned to you": "Este tile não está mais atribuído a você.",
    "admin only": "Apenas administradores podem realizar esta ação.",
    "user inactive": "Usuário desativado. Contate o admin.",
    "user not found or inactive": "Usuário inválido ou desativado.",
    "username already exists": "Nome de usuário já existe.",
}


def _friendly(message: str) -> str:
    return _FRIENDLY_ERRORS.get(message, message)


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


FRONTEND_DIR = FsPath(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Optional MBTiles tile source
    mbtiles_path = (get_config().get("tileserver") or {}).get("mbtiles_path")
    if mbtiles_path:
        p = FsPath(mbtiles_path)
        if not p.is_absolute():
            p = FsPath(__file__).resolve().parent / p
        if p.exists():
            mbtiles_service.open_mbtiles(p)
    try:
        yield
    finally:
        mbtiles_service.close_mbtiles()


app = FastAPI(title="TileClass", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Attach a human-readable Portuguese message while preserving the
    structured `detail` payload that the frontend already relies on."""
    detail = exc.detail
    if isinstance(detail, dict):
        key = detail.get("error") or detail.get("message")
        if key and "message" not in detail:
            detail = {**detail, "message": _friendly(key)}
        payload = {"detail": detail}
    else:
        payload = {"detail": _friendly(str(detail))}
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Intranet deployment: all assets served from same origin.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob: https://server.arcgisonline.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "script-src 'self' https://unpkg.com; "
        "worker-src 'self' blob:; "
        "connect-src 'self' https://server.arcgisonline.com; "
        "frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


async def _read_mask_body(request: Request) -> bytes:
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


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------- Auth ----------

@app.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    auth.check_login_rate_limit(ip)
    user = auth.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenOut(
        access_token=auth.make_access_token(user.id, user.username, user.role),
        refresh_token=auth.make_refresh_token(user.id),
    )


@app.post("/api/auth/refresh", response_model=TokenOut)
def refresh(body: RefreshIn):
    import jwt
    try:
        payload = auth.decode_token(body.refresh_token)
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid refresh: {e}")
    if payload.get("typ") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not a refresh token")
    user_id = int(payload["sub"])
    from .database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, active FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user inactive")
    return TokenOut(
        access_token=auth.make_access_token(row["id"], row["username"], row["role"]),
        refresh_token=auth.make_refresh_token(row["id"]),
    )


@app.get("/api/auth/me", response_model=UserOut)
def me(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return UserOut(id=user.id, username=user.username, role=user.role)


@app.post("/api/auth/logout")
def logout(request: Request, user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Revoke access (and optionally refresh) tokens by jti."""
    creds = request.headers.get("authorization", "")
    token = creds.split(" ", 1)[1] if creds.startswith("Bearer ") else None
    if token:
        try:
            p = auth.decode_token(token)
            if p.get("jti"):
                auth.blacklist_token(p["jti"], user.id, int(p.get("exp", 0)))
        except _jwt.PyJWTError:
            pass
    return {"ok": True}


# ---------- Config ----------

@app.get("/api/config/classes", response_model=list[ClassOut])
def config_classes():
    return get_config()["classes"]


@app.get("/api/config/tileserver")
def config_tileserver():
    cfg = get_config()
    ts = cfg.get("tileserver") or {}
    ts2 = cfg.get("tileserver_secondary") or {}
    min_zoom = max_zoom = None
    if mbtiles_service.is_open():
        fmt = mbtiles_service.tile_format()
        url = f"/api/xyz/{{z}}/{{x}}/{{y}}.{fmt}"
        min_zoom, max_zoom = mbtiles_service.zoom_range()
    else:
        url = ts.get("url_template", "")
    return {
        "url_template": url,
        "secondary_url_template": ts2.get("url_template"),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "secondary_max_zoom": ts2.get("max_zoom", 22),
    }


@app.get("/api/xyz/{z}/{x}/{y}.{ext}")
def mbtiles_xyz(z: int, x: int, y: int, ext: str):
    """Serve MBTiles tiles by XYZ (TMS conversion internal)."""
    if not mbtiles_service.is_open():
        raise HTTPException(status_code=404, detail="mbtiles not configured")
    if ext.lower() != mbtiles_service.tile_format():
        raise HTTPException(status_code=404, detail="wrong extension")
    data = mbtiles_service.get_tile(z, x, y)
    if data is None:
        return Response(status_code=204)
    media = "image/webp" if ext.lower() == "webp" else f"image/{ext.lower()}"
    return Response(
        content=data,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# ---------- Tiles (operator) ----------

@app.get("/api/tiles/next")
def next_tile(user: auth.CurrentUser = Depends(auth.get_current_user)):
    t = tile_service.get_next_tile(user.id)
    if not t:
        return Response(status_code=204)
    return t


@app.get("/api/tiles/assigned")
def my_assigned_tile(user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Return the tile currently assigned to this user (resume target), or 204.
    Does NOT assign a new tile from the queue."""
    t = tile_service.get_resume_tile(user.id)
    if not t:
        return Response(status_code=204)
    return t


@app.get("/api/tiles/next-preview")
def next_tile_preview(user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Peek without assigning — used by the frontend to pre-load the next image."""
    t = tile_service.peek_next_tile(user.id)
    if not t:
        return Response(status_code=204)
    return t


@app.get("/api/me/stats-today")
def my_stats_today(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return {"count": tile_service.today_classify_count(user.id)}


@app.get("/api/tiles/queue-stats")
def queue_stats(_: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.queue_stats()


@app.get("/api/tiles/{tile_id}/history")
def tile_history(tile_id: int = Path(ge=1), _: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.tile_history(tile_id)


@app.get("/api/tiles/{tile_id}", response_model=TileOut)
def get_tile(tile_id: int = Path(ge=1), user: auth.CurrentUser = Depends(auth.get_current_user)):
    t = tile_service.get_tile(tile_id)
    if not t:
        raise HTTPException(404, "tile not found")
    return t


@app.get("/api/tiles/{tile_id}/image")
def get_tile_image(tile_id: int = Path(ge=1), user: auth.CurrentUser = Depends(auth.get_current_user)):
    img = tile_service.get_tile_image(tile_id)
    if img is None:
        raise HTTPException(404, "tile not found")
    return Response(content=img, media_type="image/png")


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


@app.post("/api/tiles/{tile_id}/classify")
async def classify(tile_id: int = Path(ge=1), *, request: Request,
                   user: auth.CurrentUser = Depends(auth.get_current_user)):
    return await _submit(tile_id, request, user)


@app.post("/api/tiles/{tile_id}/review")
async def review(tile_id: int = Path(ge=1), *, request: Request,
                 user: auth.CurrentUser = Depends(auth.get_current_user)):
    return await _submit(tile_id, request, user)


@app.post("/api/tiles/{tile_id}/report-problem")
def report_problem(body: ReportProblemIn, tile_id: int = Path(ge=1),
                   user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.report_problem(tile_id, user.id, body.note)


@app.post("/api/tiles/{tile_id}/pause")
async def pause_tile(tile_id: int = Path(ge=1), *, request: Request,
                     user: auth.CurrentUser = Depends(auth.get_current_user)):
    raw = await _read_mask_body(request)
    return tile_service.pause_tile(tile_id, user.id, raw, _expected_version(request))


@app.post("/api/tiles/{tile_id}/resume")
def resume_tile(tile_id: int = Path(ge=1),
                user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.resume_tile(tile_id, user.id)


# ---------- Admin ----------

@app.get("/api/admin/dashboard", response_model=DashboardOut)
def admin_dashboard(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.dashboard()


@app.get("/api/admin/tiles")
def admin_tiles(status: TileStatus | None = None,
                user_id: int | None = Query(default=None, ge=1),
                date_from: str | None = None, date_to: str | None = None,
                paused: bool | None = None,
                limit: int = Query(default=200, ge=1, le=1000),
                offset: int = Query(default=0, ge=0),
                _: auth.CurrentUser = Depends(auth.require_admin)):
    if date_from:
        date_from = _parse_iso_date(date_from, "date_from")
    if date_to:
        date_to = _parse_iso_date(date_to, "date_to")
    status_v = status.value if status else None
    items = admin_service.list_tiles(
        status=status_v, user_id=user_id, date_from=date_from, date_to=date_to,
        paused=paused, limit=limit, offset=offset,
    )
    total = admin_service.count_tiles(
        status=status_v, user_id=user_id, date_from=date_from, date_to=date_to,
        paused=paused,
    )
    # Pagination metadata in headers keeps the JSON body a plain list so
    # existing clients/tests that index into it keep working.
    return JSONResponse(content=items, headers={"X-Total-Count": str(total)})


@app.get("/api/admin/tiles/{tile_id}/thumbnail")
def admin_tile_thumbnail(tile_id: int = Path(ge=1), size: int = Query(128, ge=16, le=512),
                         _: auth.CurrentUser = Depends(auth.require_admin)):
    return Response(
        content=admin_service.tile_thumbnail(tile_id, size=size),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/admin/tiles/problems")
def admin_problems(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_problems()


@app.get("/api/admin/tiles/map")
def admin_tiles_map(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_tiles_map()


@app.post("/api/admin/tiles/bulk/reset")
def admin_bulk_reset(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.reset_many(body.ids, u.id, body.reason)
    return {"affected": n}


@app.post("/api/admin/tiles/bulk/re-review")
def admin_bulk_rereview(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.re_review_many(body.ids, u.id, reason=body.reason)
    return {"affected": n}


@app.post("/api/admin/tiles/{tile_id}/reset")
def admin_reset(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.reset_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@app.post("/api/admin/tiles/assign")
def admin_bulk_assign(body: BulkAssignIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    """Pre-load a user's personal queue with many tiles. Every assigned tile
    enters `paused_at=now()`, so /api/tiles/next serves them FIFO when the
    user asks for more work."""
    return admin_service.assign_many(body.tile_ids, body.user_id, u.id, body.reason)


@app.post("/api/admin/tiles/{tile_id}/assign")
def admin_assign(body: AssignTileIn, tile_id: int = Path(ge=1),
                 u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.assign_operator(tile_id, body.user_id, u.id, body.reason)


@app.post("/api/admin/tiles/{tile_id}/unassign")
def admin_unassign(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                   u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.unassign_operator(tile_id, u.id, body.reason if body else None)


@app.delete("/api/admin/tiles/{tile_id}")
def admin_delete_tile(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                      u: auth.CurrentUser = Depends(auth.require_admin)):
    """Permanent delete. Only allowed for tiles already flagged as `problem`
    so an operator's report always precedes the admin's removal."""
    return admin_service.delete_tile(tile_id, u.id, body.reason if body else None)


@app.post("/api/admin/tiles/{tile_id}/re-review")
def admin_re_review(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                     u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.re_review_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@app.post("/api/admin/tiles/bulk/block")
def admin_bulk_block(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.block_many(body.ids, u.id, body.reason)
    return {"affected": n}


@app.post("/api/admin/tiles/bulk/unblock")
def admin_bulk_unblock(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.unblock_many(body.ids, u.id, body.reason)
    return {"affected": n}


@app.post("/api/admin/tiles/{tile_id}/block")
def admin_block(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.block_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@app.post("/api/admin/tiles/{tile_id}/unblock")
def admin_unblock(body: ResetReasonIn | None = None, tile_id: int = Path(ge=1),
                  u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.unblock_tile(tile_id, u.id, body.reason if body else None)
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_users()


@app.post("/api/admin/users")
def admin_create_user(body: CreateUserIn, _: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.create_user(body.username, body.password, body.role)


@app.patch("/api/admin/users/{user_id}/active")
def admin_set_user_active(body: SetActiveIn, user_id: int = Path(ge=1),
                          u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_active(user_id, body.active, u.id)


@app.patch("/api/admin/users/{user_id}/can-review")
def admin_set_user_can_review(body: SetCanReviewIn, user_id: int = Path(ge=1),
                              u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_can_review(user_id, body.can_review, u.id)


# ---------- Static frontend ----------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        target = FRONTEND_DIR / path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIR / "index.html")
