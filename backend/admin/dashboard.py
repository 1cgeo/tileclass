"""Admin dashboard: aggregates totals, completion %, ETA, and per-operator stats.
Cycle durations come from action_log (assign_*→classify/review pairs minus
pause→resume intervals inside the cycle).

`project_id` is propagated through every aggregate; None means global view.
"""
from ..database import connect


def _scope(project_id: int | None,
           prefix: str = "") -> tuple[str, list]:
    """Return (' AND <prefix>project_id=?', [pid]) when scoped, ('', []) when not.
    Centralises the f-string-friendly fragment so every aggregate uses the
    same shape and there's one place to look when the schema changes."""
    if project_id is None:
        return "", []
    return f" AND {prefix}project_id=?", [project_id]


def _cycle_durations(conn, assign_action: str, done_action: str,
                     project_id: int | None = None):
    join_sql = "" if project_id is None else " LEFT JOIN tiles tt ON tt.id=action_log.tile_id"
    extra_where, project_args = _scope(project_id, prefix="tt.")
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


def class_distribution(project_id: int | None = None) -> list[dict]:
    """Pixel counts per class across every classified/reviewed tile, joined
    with the project's class metadata so the dashboard can render colored
    bars without an extra round-trip.

    Reads `tiles.class_counts` (cached at submit). Tiles not yet classified
    are NULL and contribute nothing; running `recompute_class_counts.py`
    backfills legacy DBs."""
    import json
    proj_clause, proj_args = _scope(project_id)
    where_proj = ("WHERE 1=1" + proj_clause) if proj_clause else ""
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT project_id, class_counts FROM tiles {where_proj}",
            proj_args,
        ).fetchall()
        # totals[(pid, class_id)] = pixels
        totals: dict[tuple[int, int], int] = {}
        for r in rows:
            if not r["class_counts"]:
                continue
            try:
                cc = json.loads(r["class_counts"])
            except (TypeError, ValueError):
                continue
            for cid_str, count in cc.items():
                key = (r["project_id"], int(cid_str))
                totals[key] = totals.get(key, 0) + int(count)

        # Resolve names + colors per project. One query per distinct project.
        seen_projects = {pid for (pid, _) in totals}
        names: dict[tuple[int, int], tuple[str, str]] = {}
        for pid in seen_projects:
            for c in conn.execute(
                "SELECT class_id, name, color FROM project_classes WHERE project_id=?",
                (pid,),
            ).fetchall():
                names[(pid, c["class_id"])] = (c["name"], c["color"])
    finally:
        conn.close()
    # Percentages are PER PROJECT — different projects have unrelated palettes
    # and pixel scales, so a global denominator would make the pct meaningless
    # when the dashboard isn't project-scoped.
    project_totals: dict[int, int] = {}
    for (pid, _cid), pixels in totals.items():
        project_totals[pid] = project_totals.get(pid, 0) + pixels
    out = []
    for (pid, cid), pixels in sorted(totals.items(), key=lambda kv: (-kv[1],)):
        name, color = names.get((pid, cid), (f"#{cid}", "#888888"))
        out.append({
            "project_id": pid,
            "class_id": cid,
            "name": name,
            "color": color,
            "pixels": pixels,
            "pct": round(100.0 * pixels / (project_totals[pid] or 1), 2),
        })
    return out


def feature_distribution(project_id: int | None = None) -> list[dict]:
    """Vector counterpart to class_distribution: counts feature occurrences
    by enum/boolean attribute value, scanning each tile's data_geojson once.

    Returned shape:
      [{project_id, attribute_key, value, count, pct}, ...]

    Text/number attributes are skipped — they have unbounded value spaces
    and the dashboard's bar chart needs a discrete vocabulary. Tiles whose
    body is null or unparseable are silently ignored."""
    import json
    proj_clause, proj_args = _scope(project_id)
    where_proj = ("WHERE 1=1" + proj_clause) if proj_clause else ""
    conn = connect()
    try:
        # Pull the per-project schema so we know which keys are enum/boolean.
        schema_rows = conn.execute(
            f"""SELECT pa.project_id, pa.key, pa.type, pa.options_json
                FROM project_attributes pa
                JOIN projects p ON p.id=pa.project_id
                WHERE p.kind='vector'
                  AND pa.type IN ('enum','boolean')
                  {('AND p.id=?' if project_id is not None else '')}""",
            proj_args,
        ).fetchall()
        schema_by_pid: dict[int, list[tuple[str, str]]] = {}
        for s in schema_rows:
            schema_by_pid.setdefault(s["project_id"], []).append(
                (s["key"], s["type"]),
            )
        # Tiles with non-null bodies for projects that have at least one
        # countable attribute.
        tile_rows = conn.execute(
            f"""SELECT t.project_id, t.data_geojson FROM tiles t
                JOIN projects p ON p.id=t.project_id
                WHERE p.kind='vector' AND t.data_geojson IS NOT NULL
                  {('AND t.project_id=?' if project_id is not None else '')}""",
            proj_args,
        ).fetchall()
    finally:
        conn.close()

    counts: dict[tuple[int, str, str], int] = {}
    project_totals: dict[tuple[int, str], int] = {}
    for r in tile_rows:
        pid = r["project_id"]
        keys = schema_by_pid.get(pid)
        if not keys:
            continue
        try:
            doc = json.loads(r["data_geojson"])
        except (TypeError, ValueError):
            continue
        for f in doc.get("features", []) or []:
            props = f.get("properties") or {}
            for key, _t in keys:
                value = props.get(key)
                if value is None or value == "":
                    continue
                # Stringify so booleans + enums share the same key shape.
                v = str(value).lower() if isinstance(value, bool) else str(value)
                counts[(pid, key, v)] = counts.get((pid, key, v), 0) + 1
                project_totals[(pid, key)] = project_totals.get((pid, key), 0) + 1

    out = []
    for (pid, key, value), count in sorted(
        counts.items(), key=lambda kv: (-kv[1],),
    ):
        denom = project_totals.get((pid, key), 1)
        out.append({
            "project_id": pid,
            "attribute_key": key,
            "value": value,
            "count": count,
            "pct": round(100.0 * count / denom, 2),
        })
    return out


def tile_class_distribution(project_id: int | None = None) -> list[dict]:
    """Classification counterpart to class_distribution: counts tiles per
    assigned class_id, joined with project_classes for name + color.

    Returned shape mirrors class_distribution but uses `count` (tiles)
    instead of `pixels`. `data_class_id IS NOT NULL` is enough to scope the
    aggregate — only `_submit_classification` writes that column."""
    proj_clause, proj_args = _scope(project_id, prefix="t.")
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT t.project_id, t.data_class_id AS class_id,
                       pc.name, pc.color, COUNT(*) AS c
                FROM tiles t
                LEFT JOIN project_classes pc
                  ON pc.project_id=t.project_id AND pc.class_id=t.data_class_id
                WHERE t.data_class_id IS NOT NULL{proj_clause}
                GROUP BY t.project_id, t.data_class_id, pc.name, pc.color
                ORDER BY c DESC""",
            proj_args,
        ).fetchall()
    finally:
        conn.close()
    # Per-project percentages (see class_distribution — global denominator
    # mixes unrelated projects).
    project_totals: dict[int, int] = {}
    for r in rows:
        project_totals[r["project_id"]] = project_totals.get(r["project_id"], 0) + r["c"]
    return [
        {
            "project_id": r["project_id"],
            "class_id": r["class_id"],
            "name": r["name"] or f"#{r['class_id']}",
            "color": r["color"] or "#888888",
            "count": r["c"],
            "pct": round(100.0 * r["c"] / (project_totals[r["project_id"]] or 1), 2),
        }
        for r in rows
    ]


def detection_distribution(project_id: int | None = None) -> list[dict]:
    """Detection counterpart: counts bounding boxes per class id by walking
    each tile's data_geojson once (boxes are Polygon features with
    `properties.class_id`).

    Returned shape mirrors class_distribution but uses `count` (boxes). Empty
    bodies, parse errors, and features missing a class id are silently
    skipped — they don't contribute to any class total."""
    import json
    proj_clause, proj_args = _scope(project_id, prefix="t.")
    conn = connect()
    try:
        tile_rows = conn.execute(
            f"""SELECT t.project_id, t.data_geojson FROM tiles t
                JOIN projects p ON p.id=t.project_id
                WHERE p.kind='detection' AND t.data_geojson IS NOT NULL
                  {proj_clause}""",
            proj_args,
        ).fetchall()
        # Build counts in Python so the same schema (Polygon + class_id) works
        # regardless of how the operator structured the feature collection.
        counts: dict[tuple[int, int], int] = {}
        for r in tile_rows:
            try:
                doc = json.loads(r["data_geojson"])
            except (TypeError, ValueError):
                continue
            for f in doc.get("features", []) or []:
                cid = (f.get("properties") or {}).get("class_id")
                if cid is None:
                    continue
                key = (r["project_id"], int(cid))
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            return []
        # Resolve names/colors with one query per distinct project.
        names: dict[tuple[int, int], tuple[str, str]] = {}
        for pid in {pid for (pid, _) in counts}:
            for c in conn.execute(
                "SELECT class_id, name, color FROM project_classes WHERE project_id=?",
                (pid,),
            ).fetchall():
                names[(pid, c["class_id"])] = (c["name"], c["color"])
    finally:
        conn.close()
    # Per-project percentages (different projects = different palettes).
    project_totals: dict[int, int] = {}
    for (pid, _cid), n in counts.items():
        project_totals[pid] = project_totals.get(pid, 0) + n
    out = []
    for (pid, cid), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        name, color = names.get((pid, cid), (f"#{cid}", "#888888"))
        out.append({
            "project_id": pid,
            "class_id": cid,
            "name": name,
            "color": color,
            "count": n,
            "pct": round(100.0 * n / (project_totals[pid] or 1), 2),
        })
    return out


def projects_stats() -> list[dict]:
    """One row per active project for the admin Projetos sidebar AND the
    cross-project breakdown on the dashboard. Bundles five aggregates in a
    single round-trip:
      - total_tiles + classified_tiles + completion_percent
      - member_count
      - rate_per_day (tiles classified in the last 7 days / 7)
      - eta_days (remaining / rate, when rate > 0)
    Per-project rate/eta let the dashboard surface projects that are stalled
    or progressing fast, instead of the misleading global average.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.id, p.name, p.description, p.kind, p.active, p.created_at,
                      COALESCE(t.total, 0) AS total_tiles,
                      COALESCE(t.classified, 0) AS classified_tiles,
                      COALESCE(m.member_count, 0) AS member_count,
                      COALESCE(r.recent, 0) AS recent_classified
               FROM projects p
               LEFT JOIN (
                   SELECT project_id,
                          COUNT(*) AS total,
                          SUM(CASE WHEN status IN ('classified','in_review','reviewed')
                                   THEN 1 ELSE 0 END) AS classified
                   FROM tiles GROUP BY project_id
               ) t ON t.project_id = p.id
               LEFT JOIN (
                   SELECT project_id, COUNT(*) AS member_count
                   FROM project_members GROUP BY project_id
               ) m ON m.project_id = p.id
               LEFT JOIN (
                   SELECT project_id, COUNT(*) AS recent FROM tiles
                   WHERE status IN ('classified','in_review','reviewed')
                     AND classified_at >= datetime('now','-7 days')
                   GROUP BY project_id
               ) r ON r.project_id = p.id
               ORDER BY p.id"""
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        total = r["total_tiles"] or 0
        classified = r["classified_tiles"] or 0
        pct = round(100.0 * classified / total, 1) if total else 0.0
        rate = round((r["recent_classified"] or 0) / 7.0, 2)
        remaining = total - classified
        eta = round(remaining / rate, 1) if rate > 0 and remaining > 0 else None
        out.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"] or "",
            "kind": r["kind"],
            "active": bool(r["active"]),
            "created_at": r["created_at"],
            "total_tiles": total,
            "classified_tiles": classified,
            "completion_percent": pct,
            "member_count": r["member_count"] or 0,
            "rate_per_day": rate,
            "eta_days": eta,
        })
    return out


def dashboard(project_id: int | None = None) -> dict:
    proj_clause, proj_args = _scope(project_id)
    where_proj = ("WHERE 1=1" + proj_clause) if proj_clause else ""

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT status, COUNT(*) c FROM tiles {where_proj} GROUP BY status",
            proj_args,
        ).fetchall()
        totals = {r["status"]: r["c"] for r in rows}
        total = sum(totals.values())
        # "Classified" for dashboard metrics means "past the classify step" —
        # includes tiles awaiting review, under review, and fully reviewed.
        # The team's primary production metric is classification throughput.
        classified_total = (
            totals.get("classified", 0)
            + totals.get("in_review", 0)
            + totals.get("reviewed", 0)
        )
        pct = round(100.0 * classified_total / total, 2) if total else 0.0
        paused_count = conn.execute(
            f"SELECT COUNT(*) c FROM tiles WHERE paused_at IS NOT NULL{proj_clause}",
            proj_args,
        ).fetchone()["c"]
        # Per-status paused breakdown — paused tiles are still counted under
        # their raw status in totals_by_status, so the dashboard surfaces
        # paused as a distinct category.
        paused_breakdown_rows = conn.execute(
            f"""SELECT status, COUNT(*) c FROM tiles
                WHERE paused_at IS NOT NULL
                  AND status IN ('in_progress','in_review'){proj_clause}
                GROUP BY status""",
            proj_args,
        ).fetchall()
        paused_by_status = {r["status"]: r["c"] for r in paused_breakdown_rows}

        daily = conn.execute(
            f"""SELECT substr(reviewed_at,1,10) d, COUNT(*) c
                FROM tiles WHERE status='reviewed' AND reviewed_at IS NOT NULL{proj_clause}
                GROUP BY d ORDER BY d DESC LIMIT 30""",
            proj_args,
        ).fetchall()

        # When project-scoped, join through tiles so SUMs only count actions
        # on tiles in this project. The COALESCE(t.id) avoids counting
        # logout-style logs (tile_id IS NULL).
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
            combined = []
            if c: combined.append(c)
            if rv: combined.append(rv)
            d["avg_seconds_per_tile"] = round(sum(combined) / len(combined), 1) if combined else 0.0
            per_op_list.append(d)

        # ETA: remaining = tiles not yet classified; rate = tiles classified
        # in the last 7 days / 7. `classified_at` is set when a tile first
        # transitions to 'classified' and is cleared on reset.
        rate_row = conn.execute(
            f"""SELECT COUNT(*) c FROM tiles
                WHERE status IN ('classified','in_review','reviewed')
                  AND classified_at >= datetime('now','-7 days'){proj_clause}""",
            proj_args,
        ).fetchone()
        rate_per_day = (rate_row["c"] or 0) / 7.0
        remaining = total - classified_total
        eta_days = round(remaining / rate_per_day, 1) if rate_per_day > 0 else None

        # Project portfolio summary. Always lists across all active projects so
        # the cross-project view shows "how many of each kind exist" — the
        # project-scoped view also gets the figure, useful as global context.
        kind_rows = conn.execute(
            "SELECT kind, COUNT(*) c FROM projects WHERE active=1 GROUP BY kind"
        ).fetchall()
        projects_by_kind = {r["kind"]: r["c"] for r in kind_rows}
        project_count = sum(projects_by_kind.values())

        # Cross-project only: tile count per kind. With a single project picked
        # the figure would just equal `total_tiles`, so we omit it to keep the
        # payload honest about scope.
        if project_id is None:
            tile_kind_rows = conn.execute(
                """SELECT p.kind, COUNT(t.id) c FROM projects p
                   LEFT JOIN tiles t ON t.project_id=p.id
                   WHERE p.active=1 GROUP BY p.kind"""
            ).fetchall()
            tiles_by_kind = {r["kind"]: r["c"] for r in tile_kind_rows}
            project_kind = None
        else:
            tiles_by_kind = {}
            project_kind_row = conn.execute(
                "SELECT kind FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            project_kind = project_kind_row["kind"] if project_kind_row else None
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
        "project_count": project_count,
        "projects_by_kind": projects_by_kind,
        "tiles_by_kind": tiles_by_kind,
        "project_kind": project_kind,
    }
