"""JWT + bcrypt auth. HS256 tokens, 8h access / 24h refresh."""
import os
import time
import secrets
from datetime import datetime, timezone
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import get_config
from .database import connect, transaction

_bearer = HTTPBearer(auto_error=False)

ALG = "HS256"
_DEFAULT_SECRETS = {"trocar-em-producao-por-secret-forte", "", "changeme", "secret"}


def _secret() -> str:
    s = get_config()["auth"]["jwt_secret"]
    # Fail closed: the YAML placeholder must never be used in production.
    if s in _DEFAULT_SECRETS and os.environ.get("TILECLASS_ALLOW_DEFAULT_SECRET") != "1":
        raise RuntimeError(
            "refusing to use default jwt_secret; set a strong value in config.yaml "
            "(set TILECLASS_ALLOW_DEFAULT_SECRET=1 only for tests)"
        )
    return s


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _make_token(payload: dict, hours: int) -> str:
    now = int(time.time())
    payload = {**payload, "iat": now, "exp": now + hours * 3600, "jti": secrets.token_urlsafe(12)}
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


# Rate limit: persisted in SQLite so it survives restarts and cannot be
# bypassed by cycling tabs/workers. 5/min window per IP.
_WINDOW = 60.0
_MAX_ATTEMPTS = 5


def check_login_rate_limit(ip: str) -> None:
    if os.environ.get("TILECLASS_DISABLE_RATE_LIMIT") == "1":
        return
    now = time.time()
    import random
    with transaction("IMMEDIATE") as conn:
        # Amortize cleanup: only 2% of logins pay the DELETE cost.
        if random.random() < 0.02:
            conn.execute("DELETE FROM rate_limit WHERE attempted_at < ?", (now - _WINDOW,))
        row = conn.execute(
            "SELECT COUNT(*) c FROM rate_limit WHERE ip=? AND attempted_at >= ?",
            (ip, now - _WINDOW),
        ).fetchone()
        if row and row["c"] >= _MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="too many login attempts")
        conn.execute("INSERT INTO rate_limit(ip, attempted_at) VALUES (?, ?)", (ip, now))


def reset_rate_limits() -> None:
    """Clear all rate-limit state. Used by tests; safe to call at runtime."""
    try:
        with transaction("IMMEDIATE") as conn:
            conn.execute("DELETE FROM rate_limit")
    except Exception:
        # Schema may not exist yet in tests that reset before init_db.
        pass


# Revoked JTIs are cached in-process to avoid a DB round-trip on every
# authenticated request. The set is rebuilt lazily after each logout and
# kept small by purging expired entries.
_blacklist_cache: set[str] | None = None


def _load_blacklist_cache() -> set[str]:
    now = time.time()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT jti FROM token_blacklist WHERE expires_at >= ?", (now,)
        ).fetchall()
    finally:
        conn.close()
    return {r["jti"] for r in rows}


def blacklist_token(jti: str, user_id: int, exp: int) -> None:
    global _blacklist_cache
    with transaction("IMMEDIATE") as conn:
        conn.execute("DELETE FROM token_blacklist WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT OR IGNORE INTO token_blacklist(jti, user_id, expires_at) VALUES (?,?,?)",
            (jti, user_id, exp),
        )
    if _blacklist_cache is not None:
        _blacklist_cache.add(jti)


def is_blacklisted(jti: str | None) -> bool:
    global _blacklist_cache
    if not jti:
        return False
    if _blacklist_cache is None:
        _blacklist_cache = _load_blacklist_cache()
    return jti in _blacklist_cache


def _reset_blacklist_cache() -> None:
    """Test hook."""
    global _blacklist_cache
    _blacklist_cache = None


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
    if is_blacklisted(payload.get("jti")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
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
