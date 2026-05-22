"""Tile queue, assignment (atomic), classify/review/problem transitions."""
from datetime import datetime, timezone
import json
from fastapi import HTTPException
from .database import connect, transaction, log_action, now_iso
from .mask_utils import encode_mask, decode_mask, empty_mask_png, validate_submission, validate_partial, class_counts
from . import vector_utils, classify_utils, detection_utils
from . import mask_tile_service, project_service


# Base SELECT used everywhere a tile is returned to the client. The LEFT JOINs
# pull the classifier's/reviewer's/assignee's username so the review banner
# ("Classificado por <nome>") and the admin table's operator column can render
# in one round-trip. Without `assigned_to_username`, admin's in-place refresh
# after pause/assign shows a blank operator even though the tile is still
# assigned.
_TILE_SELECT = (
    "SELECT tiles.*, uc.username AS classified_by_username, "
    "ur.username AS reviewed_by_username, "
    "ua.username AS assigned_to_username "
    "FROM tiles "
    "LEFT JOIN users uc ON uc.id=tiles.classified_by "
    "LEFT JOIN users ur ON ur.id=tiles.reviewed_by "
    "LEFT JOIN users ua ON ua.id=tiles.assigned_to"
)


# Order in which the "resume" pick is chosen when the user has multiple tiles
# assigned. Rules, in order:
#   1. Non-paused tile (actively in progress) wins over any paused one.
#   2. Among paused tiles, a manual pause beats a queue pause. We detect
#      this by looking up the most recent `pause` log for the tile+user:
#      manual pauses are logged with detail=NULL, admin bulk-assigns log
#      detail='queue' (admin_service.assign_many). A user who paused a tile
#      manually expects to resume THAT tile, not a queue tile the admin
#      added later with a smaller id.
#   3. Oldest id last — FIFO only inside the "queue-paused" group, so
#      existing bulk-assign FIFO behavior is preserved.
_RESUME_ORDER_BY = """
  ORDER BY
    (paused_at IS NULL) DESC,
    ((SELECT detail FROM action_log
        WHERE tile_id=tiles.id AND user_id=? AND action='pause'
        ORDER BY id DESC LIMIT 1) IS NULL) DESC,
    id ASC
"""


def _row_to_tile_dict(row) -> dict:
    d = {
        "id": row["id"],
        "project_id": row["project_id"],
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
        "paused_at": row["paused_at"],
        "blocked_from": row["blocked_from"],
    }
    # If the row includes the mask blob, enrich with filled_pixels so the
    # client doesn't need a follow-up /api/tiles/{id} round-trip.
    try:
        png = row["data_png"]
    except (IndexError, KeyError):
        png = None
    if png:
        try:
            # tile_px is per-project; decoding with the wrong size raises and
            # would silently report 0 filled pixels (breaks resume/progress on
            # projects whose geometry isn't the 256 default).
            proj = project_service.get_project(row["project_id"]) or {}
            raw = decode_mask(png, int(proj.get("tile_px", 256)))
            d["filled_pixels"] = len(raw) - raw.count(b"\xff")
        except Exception:
            d["filled_pixels"] = 0
    else:
        d["filled_pixels"] = 0
    try:
        d["classified_by_username"] = row["classified_by_username"]
    except (IndexError, KeyError):
        d["classified_by_username"] = None
    try:
        d["reviewed_by_username"] = row["reviewed_by_username"]
    except (IndexError, KeyError):
        d["reviewed_by_username"] = None
    try:
        d["assigned_to_username"] = row["assigned_to_username"]
    except (IndexError, KeyError):
        d["assigned_to_username"] = None
    return d


def get_resume_tile(user_id: int, project_id: int | None = None) -> dict | None:
    """Return the tile currently assigned to the user (in_progress or in_review),
    or None. Read-only: does NOT assign from the queue and does NOT unpause.
    Used by the editor to decide whether to skip the idle screen on login.

    `project_id` scopes the lookup to a single project; omit to look across
    every project the user is touching.

    Selection order when multiple tiles are assigned: see `_RESUME_ORDER_BY`."""
    conn = connect()
    try:
        where = "tiles.assigned_to=? AND tiles.status IN ('in_progress','in_review')"
        # Placeholder order is: WHERE assigned_to=?, optional WHERE project_id=?,
        # then ORDER BY's `(SELECT ... WHERE user_id=?)` subquery. Build params
        # in SQL-positional order or the bindings shift silently.
        params: list = [user_id]
        if project_id is not None:
            where += " AND tiles.project_id=?"
            params.append(project_id)
        params.append(user_id)
        row = conn.execute(
            f"""{_TILE_SELECT}
                WHERE {where}
                {_RESUME_ORDER_BY}
                LIMIT 1""",
            params,
        ).fetchone()
        return _row_to_tile_dict(row) if row else None
    finally:
        conn.close()


def peek_next_tile(user_id: int, project_id: int) -> dict | None:
    """Read-only look-ahead: returns the tile the user would get next from the
    given project, without assigning. Pre-load helper for the editor."""
    conn = connect()
    try:
        row = conn.execute(
            f"""{_TILE_SELECT}
                WHERE tiles.assigned_to=? AND tiles.project_id=?
                  AND tiles.status IN ('in_progress','in_review')
                {_RESUME_ORDER_BY}
                LIMIT 1""",
            (user_id, project_id, user_id),
        ).fetchone()
        if row:
            return _row_to_tile_dict(row)
        if _user_can_review_project(conn, user_id, project_id):
            row = conn.execute(
                f"""{_TILE_SELECT}
                    WHERE tiles.status='classified' AND tiles.project_id=?
                      AND (tiles.classified_by IS NULL OR tiles.classified_by != ?)
                    ORDER BY tiles.classified_at LIMIT 1""",
                (project_id, user_id),
            ).fetchone()
            if row:
                return _row_to_tile_dict(row)
        row = conn.execute(
            f"{_TILE_SELECT} WHERE tiles.status='pending' AND tiles.project_id=? "
            "ORDER BY tiles.id LIMIT 1",
            (project_id,),
        ).fetchone()
        return _row_to_tile_dict(row) if row else None
    finally:
        conn.close()


def queue_stats(project_id: int | None = None) -> dict:
    """Queue counters for header display. Scoped to a project when given,
    aggregated across all when omitted."""
    conn = connect()
    try:
        where = "1=1"
        params: list = []
        if project_id is not None:
            where = "project_id=?"
            params = [project_id]
        row = conn.execute(f"SELECT COUNT(*) c FROM tiles WHERE {where}", params).fetchone()
        total = int(row["c"]) if row else 0
        row = conn.execute(
            f"SELECT COUNT(*) c FROM tiles WHERE {where} AND status='reviewed'", params
        ).fetchone()
        reviewed = int(row["c"]) if row else 0
        row = conn.execute(
            f"SELECT COUNT(*) c FROM tiles WHERE {where} AND status IN ('classified','reviewed')",
            params,
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


def today_classify_count(user_id: int, project_id: int | None = None) -> int:
    """Count classify+review actions by user today (UTC). Optionally scoped
    to a single project — useful for per-project session stats in the header."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = connect()
    try:
        if project_id is None:
            row = conn.execute(
                """SELECT COUNT(*) c FROM action_log
                   WHERE user_id=? AND action IN ('classify','review')
                     AND substr(created_at,1,10)=?""",
                (user_id, today),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) c FROM action_log a
                   JOIN tiles t ON t.id=a.tile_id
                   WHERE a.user_id=? AND a.action IN ('classify','review')
                     AND substr(a.created_at,1,10)=?
                     AND t.project_id=?""",
                (user_id, today, project_id),
            ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) if row else 0


# Tile is considered abandoned (operator closed laptop / lost connection)
# after 5 min without a heartbeat. The sweep runs lazily inside /next so
# zombie tiles are freed exactly when someone needs work.
_HEARTBEAT_TIMEOUT_SECONDS = 300


def heartbeat(tile_id: int, user_id: int) -> dict:
    """Operator's editor pings while a tile is open. Bumps last_heartbeat_at;
    no-op when the tile is paused, no longer assigned, or already finished."""
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT assigned_to, status, paused_at FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        if row["status"] not in ("in_progress", "in_review") or row["paused_at"]:
            return {"ok": False, "reason": "not_active"}
        conn.execute(
            "UPDATE tiles SET last_heartbeat_at=? WHERE id=?",
            (now_iso(), tile_id),
        )
    return {"ok": True}


def _auto_pause_stale(conn) -> int:
    """Inside an open transaction, mark every active tile whose last
    heartbeat is older than the timeout as paused (detail='auto'). Runs
    lazily on /next so the cleanup cost is paid only when someone is
    pulling work."""
    rows = conn.execute(
        f"""SELECT id, assigned_to FROM tiles
            WHERE status IN ('in_progress','in_review')
              AND paused_at IS NULL
              AND last_heartbeat_at IS NOT NULL
              AND (julianday('now') - julianday(last_heartbeat_at))*86400
                  > {_HEARTBEAT_TIMEOUT_SECONDS}"""
    ).fetchall()
    if not rows:
        return 0
    now = now_iso()
    for r in rows:
        conn.execute(
            "UPDATE tiles SET paused_at=?, version=version+1 WHERE id=?",
            (now, r["id"]),
        )
        log_action(conn, r["assigned_to"], r["id"], "pause", "auto")
    return len(rows)


def _user_can_review_project(conn, user_id: int, project_id: int) -> bool:
    """A user can review tiles in a project iff:
       - they are a global admin, OR
       - their global `can_review` flag is on AND they are a project member
         with role 'reviewer' or 'admin'.

    The global flag stays as a veto so the existing admin toggle keeps working
    while the per-project membership becomes the primary control. Plan: drop
    the global flag once the admin UI fully migrates to per-project roles."""
    row = conn.execute(
        "SELECT role, can_review FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        return False
    if row["role"] == "admin":
        return True
    if not row["can_review"]:
        return False
    member = conn.execute(
        "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
        (project_id, user_id),
    ).fetchone()
    return bool(member) and member["role"] in ("reviewer", "admin")


def get_next_tile(user_id: int, project_id: int) -> dict | None:
    """Atomically assign next tile in `project_id` to user. Review > pending.
    Never own classification.

    Personal queue: if admin pre-assigned tiles to the user (all stored with
    `paused_at` set), `/next` picks the non-paused one first; when only paused
    ones remain, the oldest is unpaused (auto-resume + `resume` log entry so
    the dashboard's assign→done pairing subtracts the queue-wait time)."""
    with transaction("IMMEDIATE") as conn:
        # Lazy zombie-tile cleanup: pulling work is the moment to free
        # tiles whose operator dropped off without explicitly pausing.
        _auto_pause_stale(conn)
        # 1) Resume: if user already has a tile assigned in this project,
        # return it. Resume is project-scoped so switching projects doesn't
        # silently dump the operator back into the previous project's tile.
        row = conn.execute(
            f"""{_TILE_SELECT}
                WHERE tiles.assigned_to=? AND tiles.project_id=?
                  AND tiles.status IN ('in_progress','in_review')
                {_RESUME_ORDER_BY}
                LIMIT 1""",
            (user_id, project_id, user_id),
        ).fetchone()
        if row:
            # Auto-resume the system pauses (admin bulk-assign 'queue', or
            # heartbeat-timeout 'auto' — operator just came back to work).
            # Manual pauses (detail=NULL) must survive /next so the user's
            # own paused timer doesn't silently restart on "Próximo".
            if row["paused_at"] is not None:
                last_pause = conn.execute(
                    """SELECT detail FROM action_log
                       WHERE tile_id=? AND user_id=? AND action='pause'
                       ORDER BY id DESC LIMIT 1""",
                    (row["id"], user_id),
                ).fetchone()
                if last_pause and last_pause["detail"] in ("queue", "auto"):
                    conn.execute(
                        "UPDATE tiles SET paused_at=NULL, version=version+1 WHERE id=?",
                        (row["id"],),
                    )
                    log_action(conn, user_id, row["id"], "resume")
                    row = conn.execute(
                        f"{_TILE_SELECT} WHERE tiles.id=?", (row["id"],)
                    ).fetchone()
            return _row_to_tile_dict(row)

        # 2) Review queue — only project members with role>=reviewer.
        row = None
        if _user_can_review_project(conn, user_id, project_id):
            row = conn.execute(
                f"""{_TILE_SELECT}
                    WHERE tiles.status='classified' AND tiles.project_id=?
                      AND (tiles.classified_by IS NULL OR tiles.classified_by != ?)
                    ORDER BY tiles.classified_at LIMIT 1""",
                (project_id, user_id),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE tiles SET status='in_review', assigned_to=?, "
                "last_heartbeat_at=? WHERE id=?",
                (user_id, now_iso(), row["id"]),
            )
            log_action(conn, user_id, row["id"], "assign_review")
            row = conn.execute(
                f"{_TILE_SELECT} WHERE tiles.id=?", (row["id"],)
            ).fetchone()
            return _row_to_tile_dict(row)

        # 3) Pending queue, scoped to the project.
        row = conn.execute(
            f"{_TILE_SELECT} WHERE tiles.status='pending' AND tiles.project_id=? "
            "ORDER BY tiles.id LIMIT 1",
            (project_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tiles SET status='in_progress', assigned_to=?, "
                "last_heartbeat_at=? WHERE id=?",
                (user_id, now_iso(), row["id"]),
            )
            log_action(conn, user_id, row["id"], "assign_classify")
            row = conn.execute(
                f"{_TILE_SELECT} WHERE tiles.id=?", (row["id"],)
            ).fetchone()
            return _row_to_tile_dict(row)

    return None


def get_tile(tile_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"{_TILE_SELECT} WHERE tiles.id=?", (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_tile_dict(row) if row else None


def get_tile_image(tile_id: int) -> bytes | None:
    """Return the raster mask body for a tile (PNG bytes); call sites that
    work with vector tiles use `get_tile_geojson` instead."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT data_png FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["data_png"]:
        return row["data_png"]
    return empty_mask_png(int(project_for_tile(tile_id).get("tile_px", 256)))


def get_tile_class_id(tile_id: int) -> int | None:
    """Classification body. Returns the assigned class_id or None when the
    tile has never been submitted / does not exist."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT data_class_id FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["data_class_id"]


def get_tile_geojson(tile_id: int) -> str | None:
    """Vector body. None when the tile has never been submitted; the empty
    FeatureCollection serves as the editor's starting point in that case."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT data_geojson FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["data_geojson"] or vector_utils.empty_feature_collection()


def project_for_tile(tile_id: int) -> dict:
    """Project record for the tile's project. Raises 404 when the tile or
    project is gone (rare — projects with tiles can't be hard-deleted)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT project_id FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
    proj = project_service.get_project(row["project_id"])
    if not proj:
        raise HTTPException(404, detail={"error": "project_not_found"})
    return proj


def _lock_tile_for_user(conn, tile_id: int, user_id: int,
                        expected_version: int | None,
                        *, version_message: str | None = None):
    """Inside an open `BEGIN IMMEDIATE` transaction, fetch the tile row and
    enforce the three preconditions every submit/pause path shares: row
    exists, the caller is the assignee, and the version matches the
    optimistic-lock token the editor sent. Returns the (status, assigned_to,
    version) row so the caller can switch on status.

    Authorization is checked BEFORE the version: a non-assignee must get 403,
    never a 409 that would leak the tile's current version / a 'your progress
    was saved' message for a tile they never held."""
    row = conn.execute(
        "SELECT status, assigned_to, version FROM tiles WHERE id=?", (tile_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "tile not found")
    if row["assigned_to"] != user_id:
        raise HTTPException(403, "not assigned to you")
    if expected_version is not None and row["version"] != expected_version:
        detail = {"error": "tile_modified", "current_version": row["version"]}
        if version_message:
            detail["message"] = version_message
        raise HTTPException(409, detail=detail)
    return row


def submit_classification(tile_id: int, user_id: int, body: bytes,
                          expected_version: int | None = None,
                          *, proj: dict | None = None) -> dict:
    """Dispatcher: raster expects raw 65536-byte mask; vector expects UTF-8
    JSON FeatureCollection. The project's `kind` decides which path runs;
    a Content-Type mismatch is caught earlier in the operator router.

    `proj` may be passed by callers that already resolved it (e.g. the
    operator router needs it to size the body) — saves a duplicate
    project_for_tile lookup on every submit."""
    if proj is None:
        proj = project_for_tile(tile_id)
    kind = proj.get("kind")
    if kind == "vector":
        return _submit_vector(tile_id, user_id, body, proj, expected_version)
    if kind == "classification":
        return _submit_classification(tile_id, user_id, body, proj, expected_version)
    if kind == "detection":
        return _submit_detection(tile_id, user_id, body, proj, expected_version)
    return _submit_raster(tile_id, user_id, body, proj, expected_version)


def _submit_raster(tile_id: int, user_id: int, raw_mask: bytes,
                   proj: dict, expected_version: int | None) -> dict:
    allowed = [c["id"] for c in proj["classes"]]
    require_complete = proj["mask_complete_required"]
    tile_px = int(proj.get("tile_px", 256))
    try:
        ok, missing = validate_submission(
            raw_mask, allowed_ids=allowed, require_complete=require_complete,
            tile_px=tile_px,
        )
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_mask", "message": str(e)})
    if not ok:
        raise HTTPException(422, detail={"error": "unfilled_pixels", "missing": missing})
    png = encode_mask(raw_mask, tile_px)
    # Cap at 4 bytes/pixel — real masks compress to < 0.5 bpp.
    if len(png) > 4 * tile_px * tile_px:
        raise HTTPException(413, detail={"error": "mask_too_large"})

    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(
            conn, tile_id, user_id, expected_version,
            version_message=("Este tile foi alterado por um admin enquanto você "
                             "trabalhava. Seu progresso foi salvo localmente."),
        )
        # Pixel-count cache for the dashboard's class-distribution panel —
        # avoids re-decoding the mask on every aggregate.
        cc_json = json.dumps(class_counts(raw_mask), separators=(",", ":"))
        status = row["status"]
        if status == "in_progress":
            conn.execute(
                """UPDATE tiles SET status='classified', data_png=?, classified_by=?,
                   classified_at=?, assigned_to=NULL, paused_at=NULL, version=version+1,
                   class_counts=? WHERE id=?""",
                (png, user_id, now_iso(), cc_json, tile_id),
            )
            log_action(conn, user_id, tile_id, "classify")
        elif status == "in_review":
            conn.execute(
                """UPDATE tiles SET status='reviewed', data_png=?, reviewed_by=?,
                   reviewed_at=?, assigned_to=NULL, paused_at=NULL, version=version+1,
                   class_counts=? WHERE id=?""",
                (png, user_id, now_iso(), cc_json, tile_id),
            )
            log_action(conn, user_id, tile_id, "review")
        else:
            raise HTTPException(409, f"invalid state for submit: {status}")
    # Mask just changed and the new status is visible in the overlay
    # (classified or reviewed). Invalidate after commit so a failed submit
    # leaves the cache untouched.
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True}


# Cap on the serialized FeatureCollection. ~1MB is generous given a tile is
# 640m × 640m and the operator is hand-drawing — an operator hitting this
# probably has a runaway editing bug.
_MAX_VECTOR_BODY_BYTES = 1_000_000


def _submit_vector(tile_id: int, user_id: int, body: bytes,
                   proj: dict, expected_version: int | None) -> dict:
    if len(body) > _MAX_VECTOR_BODY_BYTES:
        raise HTTPException(413, detail={"error": "body_too_large"})
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, detail={"error": "invalid_utf8"})
    ok, payload = vector_utils.validate_submission(
        text, proj.get("attributes") or [],
        topology_required=bool(proj.get("topology_required")),
    )
    if not ok:
        raise HTTPException(422, detail={"error": "invalid_features", **payload})
    fcount = len(payload["doc"]["features"])

    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(conn, tile_id, user_id, expected_version)
        status = row["status"]
        if status == "in_progress":
            conn.execute(
                """UPDATE tiles SET status='classified', data_geojson=?,
                   feature_count=?, classified_by=?, classified_at=?,
                   assigned_to=NULL, paused_at=NULL, version=version+1
                   WHERE id=?""",
                (text, fcount, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "classify")
        elif status == "in_review":
            conn.execute(
                """UPDATE tiles SET status='reviewed', data_geojson=?,
                   feature_count=?, reviewed_by=?, reviewed_at=?,
                   assigned_to=NULL, paused_at=NULL, version=version+1
                   WHERE id=?""",
                (text, fcount, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "review")
        else:
            raise HTTPException(409, f"invalid state for submit: {status}")
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True}


def _submit_detection(tile_id: int, user_id: int, body: bytes,
                      proj: dict, expected_version: int | None) -> dict:
    if len(body) > _MAX_VECTOR_BODY_BYTES:
        raise HTTPException(413, detail={"error": "body_too_large"})
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, detail={"error": "invalid_utf8"})
    allowed = [c["id"] for c in proj["classes"]]
    ok, payload = detection_utils.validate_submission(
        text, allowed, box_required=bool(proj.get("box_required")),
    )
    if not ok:
        raise HTTPException(422, detail={"error": "invalid_boxes", **payload})
    doc = payload["doc"]
    fcount = len(doc["features"])
    cc_json = json.dumps(detection_utils.class_counts(doc), separators=(",", ":"))

    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(conn, tile_id, user_id, expected_version)
        status = row["status"]
        if status == "in_progress":
            conn.execute(
                """UPDATE tiles SET status='classified', data_geojson=?,
                   feature_count=?, class_counts=?, classified_by=?, classified_at=?,
                   assigned_to=NULL, paused_at=NULL, version=version+1
                   WHERE id=?""",
                (text, fcount, cc_json, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "classify")
        elif status == "in_review":
            conn.execute(
                """UPDATE tiles SET status='reviewed', data_geojson=?,
                   feature_count=?, class_counts=?, reviewed_by=?, reviewed_at=?,
                   assigned_to=NULL, paused_at=NULL, version=version+1
                   WHERE id=?""",
                (text, fcount, cc_json, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "review")
        else:
            raise HTTPException(409, f"invalid state for submit: {status}")
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True}


# Classification body is a single int; cap the JSON envelope at 1 KB so a
# malformed payload can't blow up the worker.
_MAX_CLASSIFICATION_BODY_BYTES = 1024


def _submit_classification(tile_id: int, user_id: int, body: bytes,
                           proj: dict, expected_version: int | None) -> dict:
    if len(body) > _MAX_CLASSIFICATION_BODY_BYTES:
        raise HTTPException(413, detail={"error": "body_too_large"})
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, detail={"error": "invalid_utf8"})
    allowed = [c["id"] for c in proj["classes"]]
    try:
        class_id = classify_utils.parse_class_id(text, allowed)
    except ValueError as e:
        raise HTTPException(422, detail={"error": "invalid_class", "message": str(e)})

    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(conn, tile_id, user_id, expected_version)
        status = row["status"]
        if status == "in_progress":
            conn.execute(
                """UPDATE tiles SET status='classified', data_class_id=?,
                   classified_by=?, classified_at=?, assigned_to=NULL,
                   paused_at=NULL, version=version+1 WHERE id=?""",
                (class_id, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "classify")
        elif status == "in_review":
            conn.execute(
                """UPDATE tiles SET status='reviewed', data_class_id=?,
                   reviewed_by=?, reviewed_at=?, assigned_to=NULL,
                   paused_at=NULL, version=version+1 WHERE id=?""",
                (class_id, user_id, now_iso(), tile_id),
            )
            log_action(conn, user_id, tile_id, "review")
        else:
            raise HTTPException(409, f"invalid state for submit: {status}")
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True, "class_id": class_id}


_MAX_PROBLEM_NOTE = 2000


def request_changes(tile_id: int, user_id: int, note: str) -> dict:
    """Reviewer kicks the tile back to the classification queue with a note.
    The classifier (or whoever picks the tile next) sees the note attached.
    Mask is preserved — unlike report_problem, the work isn't wiped."""
    note = (note or "").strip()
    if not note:
        raise HTTPException(400, detail={"error": "empty_note",
                                          "message": "Nota é obrigatória."})
    if len(note) > _MAX_PROBLEM_NOTE:
        note = note[:_MAX_PROBLEM_NOTE]
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        if row["status"] != "in_review":
            raise HTTPException(409, f"invalid state for request_changes: {row['status']}")
        # Status returns to pending so anyone can pick it up; classified_by
        # stays so dashboard pairs the rework against the original classifier.
        conn.execute(
            """UPDATE tiles SET status='pending', assigned_to=NULL,
               paused_at=NULL, version=version+1 WHERE id=?""",
            (tile_id,),
        )
        log_action(conn, user_id, tile_id, "request_changes", note)
    # Status moves out of in_review (visible) so refresh the overlay tiles
    # that included this tile's mask.
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True}


def latest_review_note(tile_id: int) -> dict | None:
    """Most recent `request_changes` note still relevant to the current
    cycle. A note is "live" only while the tile's `classified_at` is set
    AND the note was logged after that timestamp — admin reset clears
    `classified_at`, which retires any pre-reset notes so the next
    classifier doesn't see ghost feedback from a discarded cycle."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT a.detail, a.created_at, u.username
               FROM action_log a
               JOIN tiles t ON t.id=a.tile_id
               LEFT JOIN users u ON u.id=a.user_id
               WHERE a.tile_id=? AND a.action='request_changes'
                 AND t.classified_at IS NOT NULL
                 AND a.created_at >= t.classified_at
               ORDER BY a.id DESC LIMIT 1""",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "note": row["detail"],
        "created_at": row["created_at"],
        "by_username": row["username"],
    }


def report_problem(tile_id: int, user_id: int, note: str) -> dict:
    # Cap note size so action_log.detail cannot be used to bloat the DB.
    note = (note or "").strip()
    if len(note) > _MAX_PROBLEM_NOTE:
        note = note[:_MAX_PROBLEM_NOTE]
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to, project_id FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        proj = project_service.get_project(row["project_id"]) or {}
        tile_px = int(proj.get("tile_px", 256))
        # Wipe both bodies — kind-agnostic so a future kind switch on the
        # project wouldn't leak a stale body into a fresh classify cycle.
        conn.execute(
            """UPDATE tiles SET status='problem', problem_note=?, data_png=?,
               assigned_to=NULL, paused_at=NULL, class_counts=NULL,
               data_geojson=NULL, feature_count=NULL,
               data_class_id=NULL WHERE id=?""",
            (note, empty_mask_png(tile_px), tile_id),
        )
        log_action(conn, user_id, tile_id, "report_problem", note)
    # Tile leaves the visible status set ('problem'), and its mask was wiped.
    # Drop overlay cache so the colored area disappears from the map.
    mask_tile_service.safe_invalidate_tile(tile_id)
    return {"ok": True}


def pause_tile(tile_id: int, user_id: int, body: bytes,
               expected_version: int | None = None,
               *, proj: dict | None = None) -> dict:
    """Save partial body and freeze the timer (dashboard subtracts
    pause→resume). Dispatches by project.kind: raster persists a PNG mask,
    vector persists a (possibly partial / invalid-topology) FeatureCollection.
    `proj` may be passed pre-resolved by the router to skip a duplicate lookup."""
    if proj is None:
        proj = project_for_tile(tile_id)
    kind = proj.get("kind")
    if kind == "classification":
        # Classification is a single-click submit — partial state has no
        # meaning, so pause is not supported.
        raise HTTPException(409, detail={
            "error": "pause_not_supported",
            "message": "Projetos de classification não suportam pause.",
        })
    if kind == "vector":
        return _pause_vector(tile_id, user_id, body, proj, expected_version)
    if kind == "detection":
        return _pause_detection(tile_id, user_id, body, proj, expected_version)
    return _pause_raster(tile_id, user_id, body, proj, expected_version)


def _pause_raster(tile_id: int, user_id: int, raw_mask: bytes,
                  proj: dict, expected_version: int | None) -> dict:
    allowed = [c["id"] for c in proj["classes"]]
    tile_px = int(proj.get("tile_px", 256))
    try:
        validate_partial(raw_mask, allowed_ids=allowed, tile_px=tile_px)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_mask", "message": str(e)})
    png = encode_mask(raw_mask, tile_px)
    if len(png) > 4 * tile_px * tile_px:
        raise HTTPException(413, detail={"error": "mask_too_large"})

    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(
            conn, tile_id, user_id, expected_version,
            version_message="Este tile foi alterado por um admin enquanto você trabalhava.",
        )
        if row["status"] not in ("in_progress", "in_review"):
            raise HTTPException(409, f"cannot pause from state: {row['status']}")
        conn.execute(
            "UPDATE tiles SET data_png=?, paused_at=?, version=version+1 WHERE id=?",
            (png, now_iso(), tile_id),
        )
        log_action(conn, user_id, tile_id, "pause")
        row = conn.execute(f"{_TILE_SELECT} WHERE tiles.id=?", (tile_id,)).fetchone()
    mask_tile_service.safe_invalidate_tile(tile_id)
    return _row_to_tile_dict(row)


def _pause_vector(tile_id: int, user_id: int, body: bytes,
                  proj: dict, expected_version: int | None) -> dict:
    """Pause for vector tiles only checks the body parses as GeoJSON.
    Attribute / topology validation is deferred to submit because pause is
    explicitly a "save my partial work" affordance — operators expect an
    incomplete state to round-trip."""
    if len(body) > _MAX_VECTOR_BODY_BYTES:
        raise HTTPException(413, detail={"error": "body_too_large"})
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, detail={"error": "invalid_utf8"})
    try:
        doc = vector_utils.parse_geojson(text)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_geojson", "message": str(e)})
    fcount = len(doc["features"])
    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(conn, tile_id, user_id, expected_version)
        if row["status"] not in ("in_progress", "in_review"):
            raise HTTPException(409, f"cannot pause from state: {row['status']}")
        conn.execute(
            "UPDATE tiles SET data_geojson=?, feature_count=?, paused_at=?, "
            "version=version+1 WHERE id=?",
            (text, fcount, now_iso(), tile_id),
        )
        log_action(conn, user_id, tile_id, "pause")
        row = conn.execute(f"{_TILE_SELECT} WHERE tiles.id=?", (tile_id,)).fetchone()
    mask_tile_service.safe_invalidate_tile(tile_id)
    return _row_to_tile_dict(row)


def _pause_detection(tile_id: int, user_id: int, body: bytes,
                     proj: dict, expected_version: int | None) -> dict:
    """Pause for detection tiles only checks the body parses as a box
    FeatureCollection (structural). class_id membership and the box_required
    gate are deferred to submit — pause is a "save my partial work" affordance."""
    if len(body) > _MAX_VECTOR_BODY_BYTES:
        raise HTTPException(413, detail={"error": "body_too_large"})
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, detail={"error": "invalid_utf8"})
    try:
        doc = detection_utils.parse_detection(text)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_geojson", "message": str(e)})
    fcount = len(doc["features"])
    cc_json = json.dumps(detection_utils.class_counts(doc), separators=(",", ":"))
    with transaction("IMMEDIATE") as conn:
        row = _lock_tile_for_user(conn, tile_id, user_id, expected_version)
        if row["status"] not in ("in_progress", "in_review"):
            raise HTTPException(409, f"cannot pause from state: {row['status']}")
        conn.execute(
            "UPDATE tiles SET data_geojson=?, feature_count=?, class_counts=?, "
            "paused_at=?, version=version+1 WHERE id=?",
            (text, fcount, cc_json, now_iso(), tile_id),
        )
        log_action(conn, user_id, tile_id, "pause")
        row = conn.execute(f"{_TILE_SELECT} WHERE tiles.id=?", (tile_id,)).fetchone()
    mask_tile_service.safe_invalidate_tile(tile_id)
    return _row_to_tile_dict(row)


def resume_tile(tile_id: int, user_id: int) -> dict:
    """Clear pause flag so the dashboard timer counts active time again."""
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT status, assigned_to, paused_at FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "tile not found")
        if row["assigned_to"] != user_id:
            raise HTTPException(403, "not assigned to you")
        if row["status"] not in ("in_progress", "in_review"):
            raise HTTPException(409, f"cannot resume from state: {row['status']}")
        if row["paused_at"] is None:
            raise HTTPException(409, "tile is not paused")
        conn.execute(
            "UPDATE tiles SET paused_at=NULL, version=version+1 WHERE id=?",
            (tile_id,),
        )
        log_action(conn, user_id, tile_id, "resume")
        row = conn.execute(f"{_TILE_SELECT} WHERE tiles.id=?", (tile_id,)).fetchone()
    return _row_to_tile_dict(row)
