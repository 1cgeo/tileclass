"""Tile queue, assignment (atomic), classify/review/problem transitions."""
from datetime import datetime, timezone
import json
from fastapi import HTTPException
from .database import connect, transaction, log_action
from .mask_utils import encode_mask, decode_mask, empty_mask_png, validate_submission


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_tile_dict(row) -> dict:
    d = {
        "id": row["id"],
        "name": row["name"],
        "bbox_west": row["bbox_west"],
        "bbox_south": row["bbox_south"],
        "bbox_east": row["bbox_east"],
        "bbox_north": row["bbox_north"],
        "status": row["status"],
        "assigned_to": row["assigned_to"],
        "classified_by": row["classified_by"],
        "reviewed_by": row["reviewed_by"],
        "version": row["version"],
    }
    # If the row includes the mask blob, enrich with filled_pixels so the
    # client doesn't need a follow-up /api/tiles/{id} round-trip.
    try:
        png = row["data_png"]
    except (IndexError, KeyError):
        png = None
    if png:
        try:
            raw = decode_mask(png)
            d["filled_pixels"] = len(raw) - raw.count(b"\xff")
        except Exception:
            d["filled_pixels"] = 0
    else:
        d["filled_pixels"] = 0
    return d


def get_resume_tile(user_id: int) -> dict | None:
    """Return the tile currently assigned to the user (in_progress or in_review),
    or None. Read-only: does NOT assign from the queue. Used by the editor to
    decide whether to skip the idle screen on login."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM tiles
               WHERE assigned_to=? AND status IN ('in_progress','in_review')
               ORDER BY id LIMIT 1""",
            (user_id,),
        ).fetchone()
        return _row_to_tile_dict(row) if row else None
    finally:
        conn.close()


def peek_next_tile(user_id: int) -> dict | None:
    """Read-only look-ahead: returns the tile the user would get next, without assigning.
    Used by the frontend to pre-load the next image while the current one is being painted."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM tiles
               WHERE assigned_to=? AND status IN ('in_progress','in_review')
               ORDER BY id LIMIT 1""",
            (user_id,),
        ).fetchone()
        if row:
            return _row_to_tile_dict(row)
        if _user_can_review(conn, user_id):
            row = conn.execute(
                """SELECT * FROM tiles
                   WHERE status='classified' AND (classified_by IS NULL OR classified_by != ?)
                   ORDER BY classified_at LIMIT 1""",
                (user_id,),
            ).fetchone()
            if row:
                return _row_to_tile_dict(row)
        row = conn.execute(
            "SELECT * FROM tiles WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        return _row_to_tile_dict(row) if row else None
    finally:
        conn.close()


def queue_stats() -> dict:
    """Global queue counters for header display."""
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM tiles").fetchone()
        total = int(row["c"]) if row else 0
        row = conn.execute("SELECT COUNT(*) c FROM tiles WHERE status='reviewed'").fetchone()
        reviewed = int(row["c"]) if row else 0
        row = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE status IN ('classified','reviewed')"
        ).fetchone()
        classified = int(row["c"]) if row else 0
    finally:
        conn.close()
    return {"total": total, "reviewed": reviewed, "classified": classified}


def tile_history(tile_id: int) -> list[dict]:
    """Return the full action_log for a tile, enriched with usernames."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT a.id, a.action, a.detail, a.created_at, a.user_id, u.username
               FROM action_log a LEFT JOIN users u ON u.id=a.user_id
               WHERE a.tile_id=? ORDER BY a.id""",
            (tile_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def today_classify_count(user_id: int) -> int:
    """Count classify+review actions by user today (UTC)."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) c FROM action_log
               WHERE user_id=? AND action IN ('classify','review')
                 AND substr(created_at,1,10)=?""",
            (user_id, today),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) if row else 0


def _user_can_review(conn, user_id: int) -> bool:
    row = conn.execute(
        "SELECT can_review, role FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        return False
    return bool(row["can_review"]) or row["role"] == "admin"


def get_next_tile(user_id: int) -> dict | None:
    """Atomically assign next tile to user. Review > pending. Never own classification."""
    with transaction("IMMEDIATE") as conn:
        # 1) Resume: if user already has a tile assigned and in-progress/in-review, return it.
        row = conn.execute(
            """SELECT * FROM tiles
               WHERE assigned_to=? AND status IN ('in_progress','in_review')
               ORDER BY id LIMIT 1""",
            (user_id,),
        ).fetchone()
        if row:
            return _row_to_tile_dict(row)

        # 2) Review queue — only operators explicitly opted-in by an admin.
        row = None
        if _user_can_review(conn, user_id):
            row = conn.execute(
                """SELECT * FROM tiles
                   WHERE status='classified' AND (classified_by IS NULL OR classified_by != ?)
                   ORDER BY classified_at LIMIT 1""",
                (user_id,),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE tiles SET status='in_review', assigned_to=? WHERE id=?",
                (user_id, row["id"]),
            )
            log_action(conn, user_id, row["id"], "assign_review")
            row = conn.execute("SELECT * FROM tiles WHERE id=?", (row["id"],)).fetchone()
            return _row_to_tile_dict(row)

        # 3) Pending queue
        row = conn.execute(
            "SELECT * FROM tiles WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tiles SET status='in_progress', assigned_to=? WHERE id=?",
                (user_id, row["id"]),
            )
            log_action(conn, user_id, row["id"], "assign_classify")
            row = conn.execute("SELECT * FROM tiles WHERE id=?", (row["id"],)).fetchone()
            return _row_to_tile_dict(row)

    return None


def get_tile(tile_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT t.*, u.username AS classified_by_username
               FROM tiles t LEFT JOIN users u ON u.id=t.classified_by
               WHERE t.id=?""",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = _row_to_tile_dict(row)
    d["classified_by_username"] = row["classified_by_username"]
    return d


def get_tile_image(tile_id: int) -> bytes | None:
    conn = connect()
    try:
        row = conn.execute("SELECT data_png FROM tiles WHERE id=?", (tile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["data_png"] or empty_mask_png()


def submit_classification(tile_id: int, user_id: int, raw_mask: bytes,
                          expected_version: int | None = None) -> dict:
    try:
        ok, missing = validate_submission(raw_mask)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_mask", "message": str(e)})
    if not ok:
        raise HTTPException(422, detail={"error": "unfilled_pixels", "missing": missing})
    png = encode_mask(raw_mask)
    # Encoded PNG of a 256x256 8-bit mask never exceeds ~100KB; guard against
    # unexpected blowup so a bug cannot inflate the DB.
    if len(png) > 200_000:
        raise HTTPException(413, detail={"error": "mask_too_large"})

    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to, version FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if expected_version is not None and row["version"] != expected_version:
            raise HTTPException(
                409,
                detail={
                    "error": "tile_modified",
                    "message": "Este tile foi alterado por um admin enquanto você trabalhava. "
                               "Seu progresso foi salvo localmente.",
                    "current_version": row["version"],
                },
            )
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        status = row["status"]
        if status == "in_progress":
            conn.execute(
                """UPDATE tiles SET status='classified', data_png=?, classified_by=?,
                   classified_at=?, assigned_to=NULL, version=version+1 WHERE id=?""",
                (png, user_id, _now(), tile_id),
            )
            log_action(conn, user_id, tile_id, "classify")
        elif status == "in_review":
            conn.execute(
                """UPDATE tiles SET status='reviewed', data_png=?, reviewed_by=?,
                   reviewed_at=?, assigned_to=NULL, version=version+1 WHERE id=?""",
                (png, user_id, _now(), tile_id),
            )
            log_action(conn, user_id, tile_id, "review")
        else:
            raise HTTPException(409, f"invalid state for submit: {status}")
    return {"ok": True}


_MAX_PROBLEM_NOTE = 2000


def report_problem(tile_id: int, user_id: int, note: str) -> dict:
    # Cap note size so action_log.detail cannot be used to bloat the DB.
    note = (note or "").strip()
    if len(note) > _MAX_PROBLEM_NOTE:
        note = note[:_MAX_PROBLEM_NOTE]
    with transaction("IMMEDIATE") as conn:
        row = conn.execute("SELECT status, assigned_to FROM tiles WHERE id=?", (tile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        conn.execute(
            """UPDATE tiles SET status='problem', problem_note=?, data_png=?, assigned_to=NULL
               WHERE id=?""",
            (note, empty_mask_png(), tile_id),
        )
        log_action(conn, user_id, tile_id, "report_problem", note)
    return {"ok": True}
