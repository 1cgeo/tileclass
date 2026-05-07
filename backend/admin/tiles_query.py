"""Read-only tile listings for admin: filtered grid, map view, problems."""
from ..database import connect


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


def _build_tiles_filter(
    *, prefix: str = "",
    status: str | None = None, user_id: int | None = None,
    date_from: str | None = None, date_to: str | None = None,
    paused: bool | None = None, q: str | None = None,
    project_id: int | None = None,
) -> tuple[str, list]:
    """Compose the shared `WHERE ...` clause used by list_tiles + count_tiles.
    Returns ("" or " WHERE ...", positional args). `prefix` is the table alias
    (e.g. 't.' for the joined SELECT, '' for COUNT(*) over the bare tiles table)."""
    where: list[str] = []
    args: list = []
    if project_id is not None:
        where.append(f"{prefix}project_id=?")
        args.append(project_id)
    if status:
        where.append(f"{prefix}status=?")
        args.append(status)
    if user_id is not None:
        where.append(f"({prefix}classified_by=? OR {prefix}reviewed_by=?)")
        args.extend([user_id, user_id])
    if date_from:
        where.append(f"({prefix}classified_at >= ? OR {prefix}reviewed_at >= ?)")
        args.extend([date_from, date_from])
    if date_to:
        to_end = date_to + "T23:59:59"
        where.append(f"({prefix}classified_at <= ? OR {prefix}reviewed_at <= ?)")
        args.extend([to_end, to_end])
    if paused is True:
        where.append(f"{prefix}paused_at IS NOT NULL")
    elif paused is False:
        where.append(f"{prefix}paused_at IS NULL")
    search = _search_clause(q, args, prefix=prefix)
    if search:
        where.append(search)
    return (" WHERE " + " AND ".join(where)) if where else "", args


# Whitelist mapping `sort_by` API value → safe SQL expression.
# Never interpolate user input into ORDER BY directly — only values from this
# dict reach the query. NULLs go last so unclassified/unreviewed tiles don't
# crowd the top of asc sorts on classified_at/reviewed_at/usernames.
_TILE_SORT_COLUMNS: dict[str, str] = {
    "id": "t.id",
    "name": "t.name",
    "status": "t.status",
    "classified_by_username": "uc.username",
    "reviewed_by_username": "ur.username",
    "classified_at": "t.classified_at",
    "reviewed_at": "t.reviewed_at",
}


def _build_order_by(sort_by: str | None, sort_dir: str | None) -> str:
    col = _TILE_SORT_COLUMNS.get(sort_by or "", "t.id")
    direction = "DESC" if (sort_dir or "").lower() == "desc" else "ASC"
    nulls = "NULLS LAST" if direction == "ASC" else "NULLS FIRST"
    # Tie-break by id so pagination is stable when the sort key has duplicates.
    return f" ORDER BY {col} {direction} {nulls}, t.id ASC"


def list_tiles(status: str | None = None, user_id: int | None = None,
               date_from: str | None = None, date_to: str | None = None,
               paused: bool | None = None, q: str | None = None,
               sort_by: str | None = None, sort_dir: str | None = None,
               limit: int = 200, offset: int = 0,
               project_id: int | None = None) -> list[dict]:
    where_sql, args = _build_tiles_filter(
        prefix="t.", status=status, user_id=user_id,
        date_from=date_from, date_to=date_to, paused=paused, q=q,
        project_id=project_id,
    )
    order_sql = _build_order_by(sort_by, sort_dir)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.project_id, t.name, t.status, t.classified_by, t.reviewed_by, "
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
            + order_sql
            + " LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_tiles(status: str | None = None, user_id: int | None = None,
                date_from: str | None = None, date_to: str | None = None,
                paused: bool | None = None, q: str | None = None,
                project_id: int | None = None) -> int:
    where_sql, args = _build_tiles_filter(
        status=status, user_id=user_id,
        date_from=date_from, date_to=date_to, paused=paused, q=q,
        project_id=project_id,
    )
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM tiles" + where_sql, args).fetchone()
    finally:
        conn.close()
    return int(row["c"])


def list_tiles_map(project_id: int | None = None) -> list[dict]:
    """Compact tile list for the admin map view: id, name, status and bbox.
    No pagination — the map renders the full dataset as polygons."""
    conn = connect()
    try:
        if project_id is None:
            rows = conn.execute(
                "SELECT id, project_id, name, status, bbox_west, bbox_south, bbox_east, bbox_north, "
                "paused_at, blocked_from "
                "FROM tiles ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, name, status, bbox_west, bbox_south, bbox_east, bbox_north, "
                "paused_at, blocked_from "
                "FROM tiles WHERE project_id=? ORDER BY id",
                (project_id,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_problems(project_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if project_id is None:
            rows = conn.execute(
                """SELECT t.id, t.project_id, t.name, t.problem_note,
                          (SELECT user_id FROM action_log
                             WHERE tile_id=t.id AND action='report_problem'
                             ORDER BY id DESC LIMIT 1) reporter_id,
                          (SELECT created_at FROM action_log
                             WHERE tile_id=t.id AND action='report_problem'
                             ORDER BY id DESC LIMIT 1) reported_at
                   FROM tiles t WHERE t.status='problem' ORDER BY t.id"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.id, t.project_id, t.name, t.problem_note,
                          (SELECT user_id FROM action_log
                             WHERE tile_id=t.id AND action='report_problem'
                             ORDER BY id DESC LIMIT 1) reporter_id,
                          (SELECT created_at FROM action_log
                             WHERE tile_id=t.id AND action='report_problem'
                             ORDER BY id DESC LIMIT 1) reported_at
                   FROM tiles t WHERE t.status='problem' AND t.project_id=?
                   ORDER BY t.id""",
                (project_id,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
