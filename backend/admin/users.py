"""User management for admins: list, create, role/active/can_review toggles."""
from datetime import datetime, timezone
import json

from fastapi import HTTPException

from ..auth import hash_password
from ..database import connect, transaction, log_action


def list_users() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, username, role, active, can_review, created_at FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def set_user_can_review(user_id: int, can_review: bool, admin_id: int) -> dict:
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        conn.execute("UPDATE users SET can_review=? WHERE id=?",
                     (1 if can_review else 0, user_id))
        log_action(conn, admin_id, None, "set_user_can_review",
                   json.dumps({"user_id": user_id, "can_review": bool(can_review)}))
    return {"id": user_id, "can_review": bool(can_review)}


def create_user(username: str, password: str, role: str) -> dict:
    with transaction("IMMEDIATE") as conn:
        existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise HTTPException(409, "username already exists")
        # Admins always have review rights (role check shortcuts can_review),
        # so seed the column to keep it truthful for the users-list display.
        can_review = 1 if role == "admin" else 0
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
            "VALUES (?,?,?,1,?,?)",
            (username, hash_password(password), role, can_review,
             datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT id, username, role FROM users WHERE username=?", (username,)).fetchone()
    return dict(row)


def set_user_role(user_id: int, role: str, admin_id: int) -> dict:
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        if row["role"] == role:
            return {"id": user_id, "role": role}
        # Demoting the last active admin would lock everyone out of the panel.
        if row["role"] == "admin" and role == "operator":
            other = conn.execute(
                "SELECT COUNT(*) AS c FROM users "
                "WHERE role='admin' AND active=1 AND id != ?",
                (user_id,),
            ).fetchone()["c"]
            if other == 0:
                raise HTTPException(409, "cannot demote the last active admin")
        # Promotion auto-grants review rights so the can_review column stays
        # truthful (role check would shortcut it anyway, but the UI reads
        # the column).
        if role == "admin":
            conn.execute("UPDATE users SET role=?, can_review=1 WHERE id=?",
                         (role, user_id))
        else:
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        log_action(conn, admin_id, None, "set_user_role",
                   json.dumps({"user_id": user_id, "role": role}))
    return {"id": user_id, "role": role}


def set_user_active(user_id: int, active: bool, admin_id: int) -> dict:
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
        log_action(conn, admin_id, None, "set_user_active", json.dumps({"user_id": user_id, "active": active}))
    return {"id": user_id, "active": active}
