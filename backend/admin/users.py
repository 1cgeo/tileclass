"""User management for admins: list, create, role/active toggles.

User-level permissions are intentionally minimal:
  - `role`: 'operator' (regular user) or 'admin' (manages app config)
  - `active`: false soft-disables login

"Reviewer" is no longer a user-level property — it lives in
`project_members.role` so the same user can be a reviewer on project A and
a plain operator on project B. Admins are admins of the application, not
implicit reviewers everywhere; to review a project they must be added as a
member with role='reviewer'.
"""
from datetime import datetime, timezone
import json

from fastapi import HTTPException

from ..auth import hash_password
from ..database import connect, transaction, log_action


def list_users() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str) -> dict:
    with transaction("IMMEDIATE") as conn:
        existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise HTTPException(409, "username already exists")
        # `can_review` is kept in the schema (no migration) but always 0 — the
        # column is dead, kept to avoid a destructive ALTER on existing dbs.
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
            "VALUES (?,?,?,1,0,?)",
            (username, hash_password(password), role,
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
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        log_action(conn, admin_id, None, "set_user_role",
                   json.dumps({"user_id": user_id, "role": role}))
    return {"id": user_id, "role": role}


def set_user_active(user_id: int, active: bool, admin_id: int) -> dict:
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        # Admins cannot be deactivated directly — they must be demoted to
        # operator first. This forces an explicit "step down" rather than an
        # accidental lock-out, and removes the asymmetric "deactivate the
        # last admin" guard (admins can't go inactive at all now).
        if not active and row["role"] == "admin":
            raise HTTPException(409, {
                "error": "cannot_deactivate_admin",
                "message": "Administradores não podem ser desativados. Rebaixe para usuário antes.",
            })
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
        log_action(conn, admin_id, None, "set_user_active", json.dumps({"user_id": user_id, "active": active}))
    return {"id": user_id, "active": active}
