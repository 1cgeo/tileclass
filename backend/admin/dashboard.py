"""Admin dashboard: aggregates totals, completion %, ETA, and per-operator stats.
Cycle durations come from action_log (assign_*→classify/review pairs minus the
pause→resume intervals inside the cycle).

When `project_id` is supplied, every aggregate is scoped to that project; when
None, totals span the whole DB (legacy global view)."""
from ..database import connect


def _project_join(project_id: int | None) -> tuple[str, str, list]:
    """Return (extra_join, extra_where, args) to scope queries on tile_id-keyed
    log rows to a project. Empty strings when scoping is off."""
    if project_id is None:
        return "", "", []
    return (
        " LEFT JOIN tiles tt ON tt.id=action_log.tile_id",
        " AND tt.project_id=?",
        [project_id],
    )


def _cycle_durations(conn, assign_action: str, done_action: str,
                     project_id: int | None = None):
    """Per (user, tile) cycle duration in seconds, with pause→resume intervals
    inside the cycle subtracted. Pauses from previous (reset+reassigned) cycles
    are excluded by scoping to the latest assign timestamp."""
    join_sql, extra_where, project_args = _project_join(project_id)
    return conn.execute(
        f"""WITH cycles AS (
             SELECT action_log.user_id, action_log.tile_id,
               MAX(CASE WHEN action=? THEN created_at END) AS assign_at,
               MAX(CASE WHEN action=? THEN created_at END) AS done_at
             FROM action_log{join_sql}
             WHERE action IN (?, ?){extra_where}
             GROUP BY action_log.user_id, action_log.tile_id
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
        [assign_action, done_action, assign_action, done_action] + project_args,
    ).fetchall()


def dashboard(project_id: int | None = None) -> dict:
    pscope = "" if project_id is None else " WHERE project_id=?"
    pargs = [] if project_id is None else [project_id]
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT status, COUNT(*) c FROM tiles{pscope} GROUP BY status", pargs
        ).fetchall()
        totals = {r["status"]: r["c"] for r in rows}
        total = sum(totals.values())
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
            f"SELECT COUNT(*) c FROM tiles WHERE paused_at IS NOT NULL"
            + (" AND project_id=?" if project_id is not None else ""),
            pargs,
        ).fetchone()["c"]
        # Per-status paused breakdown so the dashboard can show paused as a
        # distinct category from active in_progress/in_review (paused tiles
        # are still counted under their raw status in totals_by_status).
        paused_breakdown_rows = conn.execute(
            f"""SELECT status, COUNT(*) c FROM tiles
                WHERE paused_at IS NOT NULL
                  AND status IN ('in_progress','in_review')
                  {' AND project_id=?' if project_id is not None else ''}
                GROUP BY status""",
            pargs,
        ).fetchall()
        paused_by_status = {r["status"]: r["c"] for r in paused_breakdown_rows}

        daily = conn.execute(
            f"""SELECT substr(reviewed_at,1,10) d, COUNT(*) c
                FROM tiles WHERE status='reviewed' AND reviewed_at IS NOT NULL
                {' AND project_id=?' if project_id is not None else ''}
                GROUP BY d ORDER BY d DESC LIMIT 30""",
            pargs,
        ).fetchall()

        # Average time per action (seconds between assign and finish) via
        # action_log self-joins. When project-scoped, the join through tiles
        # filters out actions on tiles in other projects.
        if project_id is None:
            per_op = conn.execute(
                """SELECT u.id, u.username,
                     SUM(CASE WHEN a.action='classify' THEN 1 ELSE 0 END) classified,
                     SUM(CASE WHEN a.action='review' THEN 1 ELSE 0 END) reviewed,
                     SUM(CASE WHEN a.action='report_problem' THEN 1 ELSE 0 END) problems
                   FROM users u LEFT JOIN action_log a ON a.user_id=u.id
                   GROUP BY u.id ORDER BY u.username"""
            ).fetchall()
        else:
            per_op = conn.execute(
                """SELECT u.id, u.username,
                     SUM(CASE WHEN a.action='classify' AND t.project_id=? THEN 1 ELSE 0 END) classified,
                     SUM(CASE WHEN a.action='review'   AND t.project_id=? THEN 1 ELSE 0 END) reviewed,
                     SUM(CASE WHEN a.action='report_problem' AND t.project_id=? THEN 1 ELSE 0 END) problems
                   FROM users u LEFT JOIN action_log a ON a.user_id=u.id
                   LEFT JOIN tiles t ON t.id=a.tile_id
                   GROUP BY u.id ORDER BY u.username""",
                (project_id, project_id, project_id),
            ).fetchall()

        classify_rows = _cycle_durations(conn, "assign_classify", "classify", project_id)
        review_rows = _cycle_durations(conn, "assign_review", "review", project_id)

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
            f"""SELECT COUNT(*) c FROM tiles
                WHERE status IN ('classified','in_review','reviewed')
                  AND classified_at >= datetime('now','-7 days')
                  {' AND project_id=?' if project_id is not None else ''}""",
            pargs,
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
        "paused_by_status": paused_by_status,
    }
