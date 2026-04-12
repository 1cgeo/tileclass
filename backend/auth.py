"""JWT + bcrypt auth. HS256 tokens, 8h access / 24h refresh."""
import time
from datetime import datetime, timezone
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import get_config
from .database import connect

_bearer = HTTPBearer(auto_error=False)

ALG = "HS256"


def _secret() -> str:
    return get_config()["auth"]["jwt_secret"]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _make_token(payload: dict, hours: int) -> str:
    now = int(time.time())
    payload = {**payload, "iat": now, "exp": now + hours * 3600}
    return jwt.encode(payload, _secret(), algorithm=ALG)


def make_access_token(user_id: int, username: str, role: str) -> str:
    cfg = get_config()["auth"]
    return _make_token(
        {"sub": str(user_id), "username": username, "role": role, "typ": "access"},
        cfg["access_token_expiry_hours"],
    )


def make_refresh_token(user_id: int) -> str:
    cfg = get_config()["auth"]
    return _make_token(
        {"sub": str(user_id), "typ": "refresh"},
        cfg["refresh_token_expiry_hours"],
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=[ALG])


# Rate limit: simple in-memory (IP -> list of timestamps), 5/min window
_login_attempts: dict[str, list[float]] = {}


def check_login_rate_limit(ip: str) -> None:
    import os
    if os.environ.get("TILECLASS_DISABLE_RATE_LIMIT") == "1":
        return
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < 60]
    if not recent:
        _login_attempts.pop(ip, None)
    else:
        _login_attempts[ip] = recent
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="too many login attempts")
    _login_attempts.setdefault(ip, []).append(now)


def reset_rate_limits() -> None:
    """Clear all rate-limit state. Used by tests; safe to call at runtime."""
    _login_attempts.clear()


class CurrentUser:
    def __init__(self, id: int, username: str, role: str):
        self.id = id
        self.username = username
        self.role = role


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}")
    if payload.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not an access token")
    user_id = int(payload["sub"])
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, active FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")
    return CurrentUser(row["id"], row["username"], row["role"])


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user


def authenticate(username: str, password: str) -> CurrentUser | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, password_hash, active FROM users WHERE username=?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["active"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return CurrentUser(row["id"], row["username"], row["role"])
