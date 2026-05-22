"""FastAPI app: lifespan + middleware + exception handler + static SPA.
Endpoints live under backend/routers/{auth,projects,operator,admin}.py."""
from contextlib import asynccontextmanager
from pathlib import Path as FsPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware

from . import mbtiles_service
from .database import init_db
from .routers import admin as admin_router
from .routers import auth as auth_router
from .routers import operator as operator_router
from .routers import projects as projects_router


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


FRONTEND_DIR = FsPath(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _prewarm_primary_readers()
    try:
        yield
    finally:
        mbtiles_service.close_all()


def _prewarm_primary_readers() -> None:
    """Open the primary mbtiles for every active project at startup so the
    first GET /api/projects/{id} doesn't pay a sequential file-open per layer.
    Remote-URL layers (Martin / TileServer-GL) skip this since they don't
    open as files; optional layers stay lazy."""
    from . import project_service
    from .database import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id FROM projects WHERE active=1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        proj = project_service.get_project(r["id"])
        if not proj:
            continue
        primary = project_service.layer_path(proj, "primary")
        if not primary or project_service.is_remote_layer(primary):
            continue
        try:
            mbtiles_service.get_reader(r["id"], "primary")
        except Exception:
            pass


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
        "default-src 'self'; "
        "img-src 'self' data: blob: https://server.arcgisonline.com https://*.virtualearth.net; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "script-src 'self' https://unpkg.com; "
        "worker-src 'self' blob:; "
        "connect-src 'self' https://server.arcgisonline.com https://*.virtualearth.net; "
        "frame-ancestors 'none'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(auth_router.router)
app.include_router(projects_router.router)
app.include_router(projects_router.admin_router)
app.include_router(operator_router.router)
app.include_router(admin_router.router)


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
