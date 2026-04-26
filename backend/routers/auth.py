"""Auth endpoints: login (rate-limited), refresh, me, logout (revoke jti)."""
import jwt as _jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import auth
from ..models import LoginIn, TokenOut, RefreshIn, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
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


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn):
    try:
        payload = auth.decode_token(body.refresh_token)
    except _jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid refresh: {e}")
    if payload.get("typ") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not a refresh token")
    user_id = int(payload["sub"])
    from ..database import connect
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


@router.get("/me", response_model=UserOut)
def me(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return UserOut(id=user.id, username=user.username, role=user.role)


@router.post("/logout")
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
