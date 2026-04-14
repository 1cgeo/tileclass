"""Admin panel queries: dashboard, tile listings, user management."""
from datetime import datetime, timezone
import io
import json
from fastapi import HTTPException
from PIL import Image
import numpy as np

from .database import connect, transaction, log_action
from .mask_utils import empty_mask_png, decode_mask, TILE_SIZE
from .auth import hash_password


def dashboard() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) c FROM tiles GROUP BY status").fetchall()
        totals = {r["status"]: r["c"] for r in rows}
        total = sum(totals.values())
        reviewed = totals.get("reviewed", 0)
        pct = round(100.0 * reviewed / total, 2) if total else 0.0

        daily = conn.execute(
            """SELECT substr(reviewed_at,1,10) d, COUNT(*) c
               FROM tiles WHERE status='reviewed' AND reviewed_at IS NOT NULL
               GROUP BY d ORDER BY d DESC LIMIT 30"""
        ).fetchall()

        # Average time per action (seconds between assign and finish) via action_log self-joins.
        per_op = conn.execute(
            """SELECT u.id, u.username,
                 SUM(CASE WHEN a.action='classify' THEN 1 ELSE 0 END) classified,
                 SUM(CASE WHEN a.action='review' THEN 1 ELSE 0 END) reviewed,
                 SUM(CASE WHEN a.action='report_problem' THEN 1 ELSE 0 END) problems
               FROM users u LEFT JOIN action_log a ON a.user_id=u.id
               GROUP BY u.id ORDER BY u.username"""
        ).fetchall()

        # Duration per (user, tile, kind): diff between the latest assign_* and the
        # latest classify/review of the same kind. Split into classify vs review so
        # admins can see both averages separately (global and per-operator).
        classify_rows = conn.execute(
            """SELECT user_id, tile_id,
                 (julianday(MAX(CASE WHEN action='classify' THEN created_at END))
                - julianday(MAX(CASE WHEN action='assign_classify' THEN created_at END)))*86400 AS dur
               FROM action_log
               WHERE action IN ('assign_classify','classify')
               GROUP BY user_id, tile_id
               HAVING dur IS NOT NULL AND dur > 0"""
        ).fetchall()
        review_rows = conn.execute(
            """SELECT user_id, tile_id,
                 (julianday(MAX(CASE WHEN action='review' THEN created_at END))
                - julianday(MAX(CASE WHEN action='assign_review' THEN created_at END)))*86400 AS dur
               FROM action_log
               WHERE action IN ('assign_review','review')
               GROUP BY user_id, tile_id
               HAVING dur IS NOT NULL AND dur > 0"""
        ).fetchall()

        def _avg_by_user(rows):
            agg: dict[int, list[float]] = {}
            for r in rows:
                agg.setdefault(r["user_id"], []).append(r["dur"])
            return {uid: sum(v) / len(v) for uid, v in agg.items()}

        classify_by_user = _avg_by_user(classify_rows)
        review_by_user = _avg_by_user(review_rows)

        all_classify = [r["dur"] for r in classify_rows]
        all_review = [r["dur"] for r in review_rows]
        avg_classify_seconds = round(sum(all_classify) / len(all_classify), 1) if all_classify else 0.0
        avg_review_seconds = round(sum(all_review) / len(all_review), 1) if all_review else 0.0

        per_op_list = []
        for r in per_op:
            d = dict(r)
            c = classify_by_user.get(r["id"])
            rv = review_by_user.get(r["id"])
            d["avg_classify_seconds"] = round(c, 1) if c else 0.0
            d["avg_review_seconds"] = round(rv, 1) if rv else 0.0
            # Combined (legacy) average kept for backwards compatibility
            combined = []
            if c: combined.append(c)
            if rv: combined.append(rv)
            d["avg_seconds_per_tile"] = round(sum(combined) / len(combined), 1) if combined else 0.0
            per_op_list.append(d)

        # ETA: remaining = total - reviewed; rate = tiles reviewed in last 7 days / day
        rate_row = conn.execute(
            """SELECT COUNT(*) c FROM tiles
               WHERE status='reviewed' AND reviewed_at >= datetime('now','-7 days')"""
        ).fetchone()
        rate_per_day = (rate_row["c"] or 0) / 7.0
        remaining = total - reviewed
        eta_days = round(remaining / rate_per_day, 1) if rate_per_day > 0 else None
    finally:
        conn.close()

    return {
        "totals_by_status": totals,
        "total_tiles": total,
        "completion_percent": pct,
        "daily_completed": [{"date": r["d"], "count": r["c"]} for r in daily],
        "per_operator": per_op_list,
        "avg_classify_seconds": avg_classify_seconds,
        "avg_review_seconds": avg_review_seconds,
        "rate_per_day": round(rate_per_day, 2),
        "eta_days": eta_days,
    }


def list_tiles(status: str | None = None, user_id: int | None = None,
               date_from: str | None = None, date_to: str | None = None,
               limit: int = 200, offset: int = 0) -> list[dict]:
    where = []
    args: list = []
    if status:
        where.append("t.status=?")
        args.append(status)
    if user_id is not None:
        where.append("(t.classified_by=? OR t.reviewed_by=?)")
        args.extend([user_id, user_id])
    if date_from:
        where.append("(t.classified_at >= ? OR t.reviewed_at >= ?)")
        args.extend([date_from, date_from])
    if date_to:
        to_end = date_to + "T23:59:59"
        where.append("(t.classified_at <= ? OR t.reviewed_at <= ?)")
        args.extend([to_end, to_end])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, t.status, t.classified_by, t.reviewed_by, "
            "t.assigned_to, t.classified_at, t.reviewed_at, t.problem_note, "
            "uc.username AS classified_by_username, "
            "ur.username AS reviewed_by_username, "
            "ua.username AS assigned_to_username "
            "FROM tiles t "
            "LEFT JOIN users uc ON uc.id=t.classified_by "
            "LEFT JOIN users ur ON ur.id=t.reviewed_by "
            "LEFT JOIN users ua ON ua.id=t.assigned_to"
            + where_sql
            + " ORDER BY t.id LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_tiles(status: str | None = None, user_id: int | None = None,
                date_from: str | None = None, date_to: str | None = None) -> int:
    where = []
    args: list = []
    if status: where.append("status=?"); args.append(status)
    if user_id is not None:
        where.append("(classified_by=? OR reviewed_by=?)"); args.extend([user_id, user_id])
    if date_from:
        where.append("(classified_at >= ? OR reviewed_at >= ?)"); args.extend([date_from, date_from])
    if date_to:
        to_end = date_to + "T23:59:59"
        where.append("(classified_at <= ? OR reviewed_at <= ?)"); args.extend([to_end, to_end])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM tiles" + where_sql, args).fetchone()
    finally:
        conn.close()
    return int(row["c"])


def list_problems() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT t.id, t.name, t.problem_note,
                      (SELECT user_id FROM action_log
                         WHERE tile_id=t.id AND action='report_problem'
                         ORDER BY id DESC LIMIT 1) reporter_id,
                      (SELECT created_at FROM action_log
                         WHERE tile_id=t.id AND action='report_problem'
                         ORDER BY id DESC LIMIT 1) reported_at
               FROM tiles t WHERE t.status='problem' ORDER BY t.id"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


_MAX_REASON_LEN = 500


def _clean_reason(reason: str | None) -> str | None:
    return ((reason or "").strip()[:_MAX_REASON_LEN]) or None


def reset_many(tile_ids: list[int], admin_id: int, reason: str | None = None) -> int:
    if not tile_ids:
        return 0
    empty = empty_mask_png()
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        for tid in tile_ids:
            conn.execute(
                """UPDATE tiles SET status='pending', data_png=?, assigned_to=NULL,
                   classified_by=NULL, reviewed_by=NULL, classified_at=NULL,
                   reviewed_at=NULL, problem_note=NULL, version=version+1 WHERE id=?""",
                (empty, tid),
            )
            log_action(conn, admin_id, tid, "reset", detail)
    return len(tile_ids)


def reset_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    reset_many([tile_id], admin_id, reason)


def re_review_many(tile_ids: list[int], admin_id: int, strict: bool = False,
                    reason: str | None = None) -> int:
    if not tile_ids:
        return 0
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        count = 0
        for tid in tile_ids:
            row = conn.execute("SELECT status FROM tiles WHERE id=?", (tid,)).fetchone()
            if not row:
                if strict:
                    raise HTTPException(404, "tile not found")
                continue
            if row["status"] != "reviewed":
                if strict:
                    raise HTTPException(409, "tile not in reviewed status")
                continue
            conn.execute(
                """UPDATE tiles SET status='classified', reviewed_by=NULL, reviewed_at=NULL,
                   assigned_to=NULL, version=version+1 WHERE id=?""",
                (tid,),
            )
            log_action(conn, admin_id, tid, "re_review", detail)
            count += 1
    return count


def unassign_operator(tile_id: int, admin_id: int, reason: str | None = None) -> dict:
    """Release the current operator from a tile without wiping the mask.
    in_progress -> pending; in_review -> classified. Other states are rejected."""
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        status = row["status"]
        if status == "in_progress":
            new_status = "pending"
        elif status == "in_review":
            new_status = "classified"
        else:
            raise HTTPException(
                409, f"tile is not assigned (status={status})"
            )
        conn.execute(
            "UPDATE tiles SET status=?, assigned_to=NULL, version=version+1 WHERE id=?",
            (new_status, tile_id),
        )
        log_action(conn, admin_id, tile_id, "unassign", detail)
    return {"id": tile_id, "status": new_status}


def re_review_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    re_review_many([tile_id], admin_id, strict=True, reason=reason)


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
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
            (username, hash_password(password), role, datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT id, username, role FROM users WHERE username=?", (username,)).fetchone()
    return dict(row)


def set_user_active(user_id: int, active: bool, admin_id: int) -> dict:
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "user not found")
        conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
        log_action(conn, admin_id, None, "set_user_active", json.dumps({"user_id": user_id, "active": active}))
    return {"id": user_id, "active": active}


def tile_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Return a PNG thumbnail of the classification mask colorized using class colors."""
    from .config import get_config
    conn = connect()
    try:
        row = conn.execute("SELECT data_png FROM tiles WHERE id=?", (tile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
    try:
        raw = decode_mask(row["data_png"])
    except (ValueError, OSError) as e:
        raise HTTPException(422, f"corrupted mask in db: {e}")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE)
    classes = get_config()["classes"]
    lut = np.zeros((256, 4), dtype=np.uint8)
    for c in classes:
        h = c["color"].lstrip("#")
        lut[c["id"]] = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    rgba = lut[arr]
    img = Image.fromarray(rgba, mode="RGBA")
    if size != TILE_SIZE:
        img = img.resize((size, size), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
