"""Admin panel queries: dashboard, tile listings, user management."""
from datetime import datetime, timezone
import io
import json
import math
from fastapi import HTTPException
from PIL import Image
import numpy as np

from . import mbtiles_service
from .database import connect, transaction, log_action
from .mask_utils import empty_mask_png, decode_mask, TILE_SIZE, PIXELS
from .auth import hash_password


def _cycle_durations(conn, assign_action: str, done_action: str):
    """Per (user, tile) cycle duration in seconds, with pause→resume intervals
    inside the cycle subtracted. Pauses from previous (reset+reassigned) cycles
    are excluded by scoping to the latest assign timestamp."""
    return conn.execute(
        """WITH cycles AS (
             SELECT user_id, tile_id,
               MAX(CASE WHEN action=? THEN created_at END) AS assign_at,
               MAX(CASE WHEN action=? THEN created_at END) AS done_at
             FROM action_log
             WHERE action IN (?, ?)
             GROUP BY user_id, tile_id
             HAVING done_at IS NOT NULL AND assign_at IS NOT NULL AND done_at > assign_at
           ),
           paired AS (
             SELECT user_id, tile_id, action, created_at,
               LEAD(action)     OVER (PARTITION BY user_id, tile_id ORDER BY created_at) AS na,
               LEAD(created_at) OVER (PARTITION BY user_id, tile_id ORDER BY created_at) AS nat
             FROM action_log
             WHERE action IN ('pause','resume')
           ),
           pause_in_cycle AS (
             SELECT p.user_id, p.tile_id,
               SUM((julianday(p.nat) - julianday(p.created_at))*86400) AS pause_secs
             FROM paired p
             JOIN cycles c ON c.user_id=p.user_id AND c.tile_id=p.tile_id
             WHERE p.action='pause' AND p.na='resume'
               AND p.created_at >= c.assign_at AND p.nat <= c.done_at
             GROUP BY p.user_id, p.tile_id
           )
           SELECT c.user_id, c.tile_id,
             (julianday(c.done_at) - julianday(c.assign_at))*86400
               - COALESCE(p.pause_secs, 0) AS dur
           FROM cycles c
           LEFT JOIN pause_in_cycle p USING (user_id, tile_id)
           WHERE (julianday(c.done_at) - julianday(c.assign_at))*86400
                 - COALESCE(p.pause_secs, 0) > 0""",
        (assign_action, done_action, assign_action, done_action),
    ).fetchall()


def dashboard() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) c FROM tiles GROUP BY status").fetchall()
        totals = {r["status"]: r["c"] for r in rows}
        total = sum(totals.values())
        reviewed = totals.get("reviewed", 0)
        # "Classified" for dashboard metrics means "past the classify step" —
        # includes tiles awaiting review, under review, and fully reviewed.
        # The % concluded, rate per day, and ETA all use this definition so
        # the numbers reflect classification throughput, which is the team's
        # primary production metric (review is a secondary validation step).
        classified_total = (
            totals.get("classified", 0)
            + totals.get("in_review", 0)
            + totals.get("reviewed", 0)
        )
        pct = round(100.0 * classified_total / total, 2) if total else 0.0
        paused_count = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE paused_at IS NOT NULL"
        ).fetchone()["c"]

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

        classify_rows = _cycle_durations(conn, "assign_classify", "classify")
        review_rows = _cycle_durations(conn, "assign_review", "review")

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

        # ETA: remaining = tiles not yet classified; rate = tiles classified
        # in the last 7 days / 7. `classified_at` is set when a tile first
        # transitions to 'classified' and is cleared on reset; filtering on
        # status keeps re-classified-then-problem tiles out of the rate.
        rate_row = conn.execute(
            """SELECT COUNT(*) c FROM tiles
               WHERE status IN ('classified','in_review','reviewed')
                 AND classified_at >= datetime('now','-7 days')"""
        ).fetchone()
        rate_per_day = (rate_row["c"] or 0) / 7.0
        remaining = total - classified_total
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
        "paused_count": paused_count,
    }


def _like_pattern(q: str) -> str:
    """Escape % and _ for SQL LIKE; use ESCAPE '\\' on the query side."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search_clause(q: str | None, args: list, prefix: str = "") -> str | None:
    """Return a SQL fragment matching id or partial name; appends args."""
    if not q:
        return None
    pat = _like_pattern(q)
    args.extend([pat, pat])
    return f"({prefix}name LIKE ? ESCAPE '\\' OR CAST({prefix}id AS TEXT) LIKE ? ESCAPE '\\')"


def list_tiles(status: str | None = None, user_id: int | None = None,
               date_from: str | None = None, date_to: str | None = None,
               paused: bool | None = None, q: str | None = None,
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
    if paused is True:
        where.append("t.paused_at IS NOT NULL")
    elif paused is False:
        where.append("t.paused_at IS NULL")
    search = _search_clause(q, args, prefix="t.")
    if search:
        where.append(search)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, t.status, t.classified_by, t.reviewed_by, "
            "t.assigned_to, t.classified_at, t.reviewed_at, t.problem_note, "
            "t.paused_at, "
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
                date_from: str | None = None, date_to: str | None = None,
                paused: bool | None = None, q: str | None = None) -> int:
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
    if paused is True:
        where.append("paused_at IS NOT NULL")
    elif paused is False:
        where.append("paused_at IS NULL")
    search = _search_clause(q, args)
    if search:
        where.append(search)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM tiles" + where_sql, args).fetchone()
    finally:
        conn.close()
    return int(row["c"])


def list_tiles_map() -> list[dict]:
    """Compact tile list for the admin map view: id, name, status and bbox.
    No pagination — the map renders the full dataset as polygons."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, status, bbox_west, bbox_south, bbox_east, bbox_north, "
            "paused_at, blocked_from "
            "FROM tiles ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                   reviewed_at=NULL, problem_note=NULL, paused_at=NULL,
                   version=version+1 WHERE id=?""",
                (empty, tid),
            )
            log_action(conn, admin_id, tid, "reset", detail)
    return len(tile_ids)


def reset_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    reset_many([tile_id], admin_id, reason)


def report_problem_many(tile_ids: list[int], admin_id: int, note: str) -> dict:
    """Admin flags N tiles as 'problem' with a shared note.

    Mirrors the operator-side report: wipes the mask and frees the slot.
    Accepts any current state — blocked tiles get their blocked_from cleared
    since the state machine leaves that branch."""
    if not tile_ids:
        return {"affected": 0}
    note = (note or "").strip()
    if not note:
        raise HTTPException(400, "note is required")
    if len(note) > 2000:
        note = note[:2000]
    empty = empty_mask_png()
    with transaction("IMMEDIATE") as conn:
        count = 0
        for tid in tile_ids:
            row = conn.execute("SELECT id FROM tiles WHERE id=?", (tid,)).fetchone()
            if not row:
                continue
            conn.execute(
                """UPDATE tiles SET status='problem', problem_note=?, data_png=?,
                   assigned_to=NULL, paused_at=NULL, blocked_from=NULL,
                   version=version+1 WHERE id=?""",
                (note, empty, tid),
            )
            log_action(conn, admin_id, tid, "report_problem", note)
            count += 1
    return {"affected": count}


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
                   assigned_to=NULL, paused_at=NULL, version=version+1 WHERE id=?""",
                (tid,),
            )
            log_action(conn, admin_id, tid, "re_review", detail)
            count += 1
    return count


def assign_many(tile_ids: list[int], user_id: int, admin_id: int,
                reason: str | None = None) -> dict:
    """Admin hand-picks an operator/reviewer for one or many tiles. Each tile
    is assigned in `paused_at=now()` state so it queues on the user's personal
    fifo without disturbing whatever they're currently working on; `/api/tiles/next`
    picks the oldest paused tile and auto-resumes it when the user asks for
    more work.

    Atomic: if any tile fails validation (wrong status, already assigned to
    someone else, reviewer==classifier, etc.), nothing is written.
    """
    if not tile_ids:
        return {"affected": 0, "ids": []}
    detail = _clean_reason(reason)
    now = _now_iso()
    with transaction("IMMEDIATE") as conn:
        user = conn.execute(
            "SELECT id, active, role, can_review FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(404, "user not found")
        if not user["active"]:
            raise HTTPException(409, "user is inactive")
        if user["role"] == "admin":
            raise HTTPException(409, "cannot assign tiles to an admin")

        # Validate every tile up-front so a bad one doesn't half-assign the lot.
        plans: list[tuple[int, str, str]] = []  # (tile_id, new_status, assign_action)
        for tid in tile_ids:
            tile = conn.execute(
                "SELECT id, status, classified_by FROM tiles WHERE id=?", (tid,)
            ).fetchone()
            if not tile:
                raise HTTPException(404, f"tile {tid} not found")
            status = tile["status"]
            if status == "pending":
                plans.append((tid, "in_progress", "assign_classify"))
            elif status == "classified":
                if not user["can_review"]:
                    raise HTTPException(
                        409, f"tile {tid} needs a reviewer (user lacks can_review)"
                    )
                if tile["classified_by"] is not None and tile["classified_by"] == user_id:
                    raise HTTPException(
                        409, f"tile {tid}: reviewer cannot be the classifier"
                    )
                plans.append((tid, "in_review", "assign_review"))
            else:
                raise HTTPException(
                    409,
                    f"tile {tid} must be pending or classified (status={status})",
                )

        items = []
        for tid, new_status, action in plans:
            conn.execute(
                """UPDATE tiles SET status=?, assigned_to=?, paused_at=?,
                   version=version+1 WHERE id=?""",
                (new_status, user_id, now, tid),
            )
            # Log under the assignee so dashboard avg_classify/avg_review pairs
            # assign→classify correctly. We also immediately log `pause` so the
            # time spent waiting in the user's personal queue is subtracted
            # from the cycle duration (closed by a `resume` when /next picks it up).
            # The `"queue"` detail distinguishes this from a manual pause so
            # `/api/tiles/next` only auto-resumes queued pauses — an operator's
            # explicit pause must stay paused until they hit resume themselves.
            log_action(conn, user_id, tid, action, detail)
            log_action(conn, user_id, tid, "pause", "queue")
            log_action(
                conn, admin_id, tid, "admin_assign",
                json.dumps({"user_id": user_id, "reason": detail}),
            )
            items.append({"id": tid, "status": new_status, "assigned_to": user_id})
    return {"affected": len(items), "items": items}


def assign_operator(tile_id: int, user_id: int, admin_id: int,
                    reason: str | None = None) -> dict:
    """Single-tile wrapper over assign_many (kept for the legacy per-tile route)."""
    result = assign_many([tile_id], user_id, admin_id, reason)
    return result["items"][0]


def delete_tile(tile_id: int, admin_id: int, reason: str | None = None) -> dict:
    """Permanently remove a tile flagged as `problem`. The status gate makes
    this a two-step process: an operator must first report the tile as
    problematic, then the admin reviews and deletes it. This avoids an admin
    clicking the wrong row and wiping a tile in active use.

    Side effects: the tile's rows in `action_log` are also deleted (no
    ON DELETE CASCADE in the schema; an orphaned tile_id would violate the
    FK). A single audit entry is written against the admin with
    `tile_id=NULL` and a JSON detail so the deletion is traceable."""
    clean = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT id, name, status FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["status"] != "problem":
            raise HTTPException(
                409,
                f"tile must be in problem status to be deleted (status={row['status']})",
            )
        conn.execute("DELETE FROM action_log WHERE tile_id=?", (tile_id,))
        conn.execute("DELETE FROM tiles WHERE id=?", (tile_id,))
        log_action(
            conn, admin_id, None, "delete_tile",
            json.dumps({"tile_id": tile_id, "name": row["name"], "reason": clean}),
        )
    return {"id": tile_id, "deleted": True}


def admin_pause_tile(tile_id: int, admin_id: int, reason: str | None = None) -> dict:
    """Pause a tile that's mid-work when the operator forgot to pause before leaving.
    Keeps the tile assigned so the operator resumes their own work when they return;
    `paused_at=now()` stops the dashboard cycle-duration timer from ticking while
    they're away.

    Logs `pause` under the operator (detail='admin') so `_cycle_durations` pairs
    it against the assign→classify/review cycle the same way a self-pause does.
    A separate `admin_pause` entry is logged under the admin for audit. We avoid
    detail='queue' because `/api/tiles/next` auto-resumes queue pauses — an
    operator returning after an admin pause should explicitly resume the tile,
    not be silently put back on the clock.
    """
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to, paused_at FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["status"] not in ("in_progress", "in_review"):
            raise HTTPException(
                409, f"tile is not in execution (status={row['status']})"
            )
        if row["assigned_to"] is None:
            raise HTTPException(409, "tile has no assignee to pause")
        if row["paused_at"] is not None:
            raise HTTPException(409, "tile is already paused")
        operator_id = row["assigned_to"]
        conn.execute(
            "UPDATE tiles SET paused_at=?, version=version+1 WHERE id=?",
            (_now_iso(), tile_id),
        )
        log_action(conn, operator_id, tile_id, "pause", "admin")
        log_action(
            conn, admin_id, tile_id, "admin_pause",
            json.dumps({"operator_id": operator_id, "reason": detail}),
        )
    return {"id": tile_id, "status": row["status"], "paused": True}


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
            "UPDATE tiles SET status=?, assigned_to=NULL, paused_at=NULL, version=version+1 WHERE id=?",
            (new_status, tile_id),
        )
        log_action(conn, admin_id, tile_id, "unassign", detail)
    return {"id": tile_id, "status": new_status}


def re_review_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    re_review_many([tile_id], admin_id, strict=True, reason=reason)


# Blockable source states: anything that isn't mid-work or already an exception.
# `in_progress`/`in_review` are rejected because blocking would strand the operator;
# `problem` is rejected per product decision (reset/delete is the problem flow);
# `blocked` is rejected because it's already blocked.
_BLOCKABLE_STATES = {"pending", "classified", "reviewed"}


def block_many(tile_ids: list[int], admin_id: int, reason: str | None = None) -> int:
    """Flip tiles to status='blocked' so they stop being distributed. The
    prior status is saved in `blocked_from` so `unblock_many` can restore it.

    Atomic: any tile failing the state gate aborts the whole batch, matching
    the behaviour of `assign_many` — admins see one error and retry rather
    than getting a partial result they can't reason about."""
    if not tile_ids:
        return 0
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        rows = conn.execute(
            f"SELECT id, status FROM tiles WHERE id IN ({','.join(['?']*len(tile_ids))})",
            tile_ids,
        ).fetchall()
        found = {r["id"]: r["status"] for r in rows}
        for tid in tile_ids:
            if tid not in found:
                raise HTTPException(404, f"tile {tid} not found")
            status = found[tid]
            if status not in _BLOCKABLE_STATES:
                if status == "blocked":
                    raise HTTPException(409, f"tile {tid} is already blocked")
                if status in ("in_progress", "in_review"):
                    raise HTTPException(
                        409, f"tile {tid} is in execution ({status}) — cannot block"
                    )
                raise HTTPException(
                    409, f"tile {tid} cannot be blocked from status={status}"
                )
        for tid in tile_ids:
            conn.execute(
                """UPDATE tiles SET blocked_from=status, status='blocked',
                   version=version+1 WHERE id=?""",
                (tid,),
            )
            log_action(conn, admin_id, tid, "block", detail)
    return len(tile_ids)


def unblock_many(tile_ids: list[int], admin_id: int, reason: str | None = None) -> int:
    """Restore each tile's status from `blocked_from`. Reject anything not
    currently blocked. `blocked_from` is guaranteed set by `block_many` so
    we read it directly."""
    if not tile_ids:
        return 0
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        rows = conn.execute(
            f"SELECT id, status, blocked_from FROM tiles "
            f"WHERE id IN ({','.join(['?']*len(tile_ids))})",
            tile_ids,
        ).fetchall()
        found = {r["id"]: r for r in rows}
        for tid in tile_ids:
            if tid not in found:
                raise HTTPException(404, f"tile {tid} not found")
            if found[tid]["status"] != "blocked":
                raise HTTPException(409, f"tile {tid} is not blocked")
        for tid in tile_ids:
            conn.execute(
                """UPDATE tiles SET status=?, blocked_from=NULL,
                   version=version+1 WHERE id=?""",
                (found[tid]["blocked_from"], tid),
            )
            log_action(conn, admin_id, tid, "unblock", detail)
    return len(tile_ids)


def block_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    block_many([tile_id], admin_id, reason)


def unblock_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    unblock_many([tile_id], admin_id, reason)


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


def tile_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Return the best thumbnail for the admin grid: colorized mask if the tile
    has any painted pixels, otherwise the satellite backdrop so empty tiles
    (pending / problem / freshly-assigned) still give the admin something to
    look at. Falls back to the transparent empty mask if MBTiles isn't open."""
    from .config import get_config
    conn = connect()
    try:
        row = conn.execute("SELECT data_png FROM tiles WHERE id=?", (tile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
    # Missing or corrupt blob renders as the empty (all-255) mask — lut[255]
    # is transparent, so the admin grid shows a blank cell instead of a
    # broken image. Fail-open is the right call: a bad thumbnail is UI noise,
    # not a data-integrity signal the admin needs to act on.
    png = row["data_png"]
    if not png:
        raw = b"\xff" * PIXELS
    else:
        try:
            raw = decode_mask(png)
        except (ValueError, OSError):
            raw = b"\xff" * PIXELS
    # Empty mask (no painted pixels) → prefer satellite backdrop when available.
    # `count(b"\xff")` is a C-level byte scan on the bytes object, cheaper than
    # any numpy detour — we only fall through to the colorized render when
    # there's at least one non-255 pixel, or MBTiles isn't open.
    if raw.count(b"\xff") == PIXELS and mbtiles_service.primary.is_open():
        try:
            return _tile_satellite_png(tile_id, size)
        except HTTPException:
            pass
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


def _lon_to_tile_x(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << z)


def _lat_to_tile_y(lat: float, z: int) -> float:
    # Web Mercator: clamp to valid range to avoid math domain errors near poles.
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    return (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (1 << z)


def _tile_satellite_png(tile_id: int, size: int = 128) -> bytes:
    """Composite satellite imagery from MBTiles over the tile bbox and return a PNG.
    Shared by the empty-mask fallback in `tile_thumbnail` and the dedicated
    `/satellite-thumbnail` endpoint. Raises 404 if MBTiles isn't open or the
    tile isn't found — callers decide whether to fall through."""
    if not mbtiles_service.primary.is_open():
        raise HTTPException(404, "satellite source not available")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT bbox_west, bbox_south, bbox_east, bbox_north FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
    w, s, e, n = row["bbox_west"], row["bbox_south"], row["bbox_east"], row["bbox_north"]
    min_z, max_z = mbtiles_service.primary.zoom_range()
    zoom = max_z if max_z is not None else 19

    x0_f = _lon_to_tile_x(w, zoom)
    x1_f = _lon_to_tile_x(e, zoom)
    # Web Mercator y grows southward: north lat → smaller y.
    y0_f = _lat_to_tile_y(n, zoom)
    y1_f = _lat_to_tile_y(s, zoom)
    x0, x1 = math.floor(x0_f), math.ceil(x1_f)
    y0, y1 = math.floor(y0_f), math.ceil(y1_f)

    TS = 256
    canvas = Image.new("RGB", ((x1 - x0) * TS, (y1 - y0) * TS), (32, 32, 32))
    for tx in range(x0, x1):
        for ty in range(y0, y1):
            data = mbtiles_service.primary.get_tile(zoom, tx, ty)
            if not data:
                continue
            try:
                src = Image.open(io.BytesIO(data)).convert("RGB")
            except (OSError, ValueError):
                continue
            canvas.paste(src, ((tx - x0) * TS, (ty - y0) * TS))

    crop_box = (
        (x0_f - x0) * TS,
        (y0_f - y0) * TS,
        (x1_f - x0) * TS,
        (y1_f - y0) * TS,
    )
    cropped = canvas.crop(crop_box)
    if cropped.size != (size, size):
        cropped = cropped.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def tile_satellite_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Public endpoint wrapper: always serve satellite, even when the mask is
    non-empty. Used by callers that explicitly want the backdrop regardless of
    mask state."""
    return _tile_satellite_png(tile_id, size)
