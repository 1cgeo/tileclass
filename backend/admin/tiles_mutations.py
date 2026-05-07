"""Admin tile mutations: reset, problem, re-review, assign/unassign, pause,
delete, block/unblock. All multi-row paths are atomic (BEGIN IMMEDIATE +
all-or-nothing); single-tile wrappers delegate to bulk so we keep one SQL path."""
import json

from fastapi import HTTPException

from functools import lru_cache

from .. import mask_tile_service, project_service
from ..database import connect, transaction, log_action, now_iso
from ..mask_utils import empty_mask_png


_MAX_REASON_LEN = 500


def _clean_reason(reason: str | None) -> str | None:
    return ((reason or "").strip()[:_MAX_REASON_LEN]) or None


@lru_cache(maxsize=None)
def _empty_png(tile_px: int) -> bytes:
    return empty_mask_png(tile_px)


def _tile_px_by_id(conn, tile_ids: list[int]) -> dict[int, int]:
    """Bulk-resolve tile_px for each tile in `tile_ids`. One SELECT joining
    tiles → project_service cache, instead of N round-trips inside the loop."""
    placeholders = ",".join("?" * len(tile_ids))
    rows = conn.execute(
        f"SELECT id, project_id FROM tiles WHERE id IN ({placeholders})",
        tile_ids,
    ).fetchall()
    px_by_pid: dict[int, int] = {}
    out: dict[int, int] = {}
    for r in rows:
        pid = r["project_id"]
        if pid not in px_by_pid:
            proj = project_service.get_project(pid) if pid else None
            px_by_pid[pid] = int((proj or {}).get("tile_px", 256))
        out[r["id"]] = px_by_pid[pid]
    return out


def reset_many(tile_ids: list[int], admin_id: int, reason: str | None = None) -> int:
    if not tile_ids:
        return 0
    detail = _clean_reason(reason)
    with transaction("IMMEDIATE") as conn:
        px_by_id = _tile_px_by_id(conn, tile_ids)
        for tid in tile_ids:
            empty = _empty_png(px_by_id.get(tid, 256))
            # class_counts cleared so the dashboard's class-distribution panel
            # stops reporting pixels of a mask that has been wiped.
            # data_geojson cleared so a vector tile round-trips back to an
            # empty FeatureCollection.
            conn.execute(
                """UPDATE tiles SET status='pending', data_png=?, data_geojson=NULL,
                   feature_count=NULL, assigned_to=NULL,
                   classified_by=NULL, reviewed_by=NULL, classified_at=NULL,
                   reviewed_at=NULL, problem_note=NULL, paused_at=NULL,
                   class_counts=NULL, version=version+1 WHERE id=?""",
                (empty, tid),
            )
            log_action(conn, admin_id, tid, "reset", detail)
    mask_tile_service.safe_invalidate_tiles(tile_ids)
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
    affected_ids: list[int] = []
    with transaction("IMMEDIATE") as conn:
        px_by_id = _tile_px_by_id(conn, tile_ids)
        for tid in tile_ids:
            if tid not in px_by_id:
                continue
            empty = _empty_png(px_by_id[tid])
            conn.execute(
                """UPDATE tiles SET status='problem', problem_note=?, data_png=?,
                   data_geojson=NULL, feature_count=NULL,
                   assigned_to=NULL, paused_at=NULL, blocked_from=NULL,
                   class_counts=NULL, version=version+1 WHERE id=?""",
                (note, empty, tid),
            )
            log_action(conn, admin_id, tid, "report_problem", note)
            affected_ids.append(tid)
    mask_tile_service.safe_invalidate_tiles(affected_ids)
    return {"affected": len(affected_ids)}


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


def re_review_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    re_review_many([tile_id], admin_id, strict=True, reason=reason)


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
    now = now_iso()
    with transaction("IMMEDIATE") as conn:
        user = conn.execute(
            "SELECT id, active, role, can_review FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(404, "user not found")
        if not user["active"]:
            raise HTTPException(409, "user is inactive")
        # Admins also act as operators/reviewers — role==admin shortcuts can_review
        # the same way `tile_service._user_can_review` does, so the queues stay
        # consistent regardless of who's working the tile.
        user_can_review = bool(user["can_review"]) or user["role"] == "admin"

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
                if not user_can_review:
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
            "SELECT id, project_id, name, status, "
            "bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM tiles WHERE id=?",
            (tile_id,),
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
        project_id = row["project_id"]
        bbox = (row["bbox_west"], row["bbox_south"], row["bbox_east"], row["bbox_north"])
    # Defensive cleanup: status was 'problem' so the mask was already wiped +
    # invalidated by report-problem. Catches any cache entry re-rendered in
    # the gap between report and delete.
    mask_tile_service.safe_invalidate_bbox(project_id, *bbox)
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
            (now_iso(), tile_id),
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


def unassign_many(tile_ids: list[int], admin_id: int, reason: str | None = None) -> int:
    """Bulk variant of `unassign_operator`. Atomic: any tile outside the
    assigned set (in_progress|in_review) aborts the batch — same contract as
    `block_many`/`assign_many`, so admins see one error and retry rather than
    a partial result they can't reason about. Mask data is preserved."""
    if not tile_ids:
        return 0
    detail = _clean_reason(reason)
    transitions = {"in_progress": "pending", "in_review": "classified"}
    with transaction("IMMEDIATE") as conn:
        rows = conn.execute(
            f"SELECT id, status FROM tiles WHERE id IN ({','.join(['?']*len(tile_ids))})",
            tile_ids,
        ).fetchall()
        found = {r["id"]: r["status"] for r in rows}
        for tid in tile_ids:
            if tid not in found:
                raise HTTPException(404, f"tile {tid} not found")
            if found[tid] not in transitions:
                raise HTTPException(
                    409, f"tile {tid} is not assigned (status={found[tid]})"
                )
        for tid in tile_ids:
            new_status = transitions[found[tid]]
            conn.execute(
                "UPDATE tiles SET status=?, assigned_to=NULL, paused_at=NULL, "
                "version=version+1 WHERE id=?",
                (new_status, tid),
            )
            log_action(conn, admin_id, tid, "unassign", detail)
    return len(tile_ids)


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
    # Visible tiles (classified/reviewed) becoming 'blocked' must drop from
    # the overlay. 'pending' source state was already invisible — invalidating
    # is a no-op there but keeps the call site uniform.
    mask_tile_service.safe_invalidate_tiles(tile_ids)
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
    # Restoring to classified/reviewed makes the mask visible again.
    mask_tile_service.safe_invalidate_tiles(tile_ids)
    return len(tile_ids)


def block_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    block_many([tile_id], admin_id, reason)


def unblock_tile(tile_id: int, admin_id: int, reason: str | None = None) -> None:
    unblock_many([tile_id], admin_id, reason)
