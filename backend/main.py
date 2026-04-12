"""FastAPI app: auth, tiles, admin, config, static frontend."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, tile_service, admin_service
from .config import get_config
from .database import init_db
from .models import (
    LoginIn, TokenOut, RefreshIn, UserOut, ClassOut, TileOut,
    ReportProblemIn, CreateUserIn, DashboardOut, BulkTileIdsIn, SetActiveIn,
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="TileClass", version="1.0.0", lifespan=lifespan)


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


# ---------- Config ----------

@app.get("/api/config/classes", response_model=list[ClassOut])
def config_classes():
    return get_config()["classes"]


@app.get("/api/config/tileserver")
def config_tileserver():
    return {"url_template": get_config()["tileserver"]["url_template"]}


# ---------- Tiles (operator) ----------

@app.get("/api/tiles/next")
def next_tile(user: auth.CurrentUser = Depends(auth.get_current_user)):
    t = tile_service.get_next_tile(user.id)
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
def tile_history(tile_id: int, _: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.tile_history(tile_id)


@app.get("/api/tiles/{tile_id}", response_model=TileOut)
def get_tile(tile_id: int, user: auth.CurrentUser = Depends(auth.get_current_user)):
    t = tile_service.get_tile(tile_id)
    if not t:
        raise HTTPException(404, "tile not found")
    return t


@app.get("/api/tiles/{tile_id}/image")
def get_tile_image(tile_id: int, user: auth.CurrentUser = Depends(auth.get_current_user)):
    img = tile_service.get_tile_image(tile_id)
    if img is None:
        raise HTTPException(404, "tile not found")
    return Response(content=img, media_type="image/png")


@app.post("/api/tiles/{tile_id}/classify")
async def classify(tile_id: int, request: Request, user: auth.CurrentUser = Depends(auth.get_current_user)):
    raw = await request.body()
    return tile_service.submit_classification(tile_id, user.id, raw)


@app.post("/api/tiles/{tile_id}/review")
async def review(tile_id: int, request: Request, user: auth.CurrentUser = Depends(auth.get_current_user)):
    raw = await request.body()
    return tile_service.submit_classification(tile_id, user.id, raw)


@app.post("/api/tiles/{tile_id}/report-problem")
def report_problem(tile_id: int, body: ReportProblemIn, user: auth.CurrentUser = Depends(auth.get_current_user)):
    return tile_service.report_problem(tile_id, user.id, body.note)


# ---------- Admin ----------

@app.get("/api/admin/dashboard", response_model=DashboardOut)
def admin_dashboard(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.dashboard()


@app.get("/api/admin/tiles")
def admin_tiles(status: str | None = None, user_id: int | None = None,
                date_from: str | None = None, date_to: str | None = None,
                limit: int = 200, offset: int = 0,
                _: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_tiles(status, user_id, date_from, date_to, limit, offset)


@app.get("/api/admin/tiles/{tile_id}/thumbnail")
def admin_tile_thumbnail(tile_id: int, size: int = 128,
                         _: auth.CurrentUser = Depends(auth.require_admin)):
    return Response(
        content=admin_service.tile_thumbnail(tile_id, size=size),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/admin/tiles/problems")
def admin_problems(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_problems()


@app.post("/api/admin/tiles/bulk/reset")
def admin_bulk_reset(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.reset_many(body.ids, u.id)
    return {"affected": n}


@app.post("/api/admin/tiles/bulk/re-review")
def admin_bulk_rereview(body: BulkTileIdsIn, u: auth.CurrentUser = Depends(auth.require_admin)):
    n = admin_service.re_review_many(body.ids, u.id)
    return {"affected": n}


@app.post("/api/admin/tiles/{tile_id}/reset")
def admin_reset(tile_id: int, u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.reset_tile(tile_id, u.id)
    return {"ok": True}


@app.post("/api/admin/tiles/{tile_id}/re-review")
def admin_re_review(tile_id: int, u: auth.CurrentUser = Depends(auth.require_admin)):
    admin_service.re_review_tile(tile_id, u.id)
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(_: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.list_users()


@app.post("/api/admin/users")
def admin_create_user(body: CreateUserIn, _: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.create_user(body.username, body.password, body.role)


@app.patch("/api/admin/users/{user_id}/active")
def admin_set_user_active(user_id: int, body: SetActiveIn,
                          u: auth.CurrentUser = Depends(auth.require_admin)):
    return admin_service.set_user_active(user_id, body.active, u.id)


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
