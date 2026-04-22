"""Dashboard tests that ACT first, then check counters reflect reality.
Replaces the tautological test_dashboard in test_admin.py that only asserts
the fixture-provided counts."""
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def _classify(client, tok, tid):
    return client.post(f"/api/tiles/{tid}/classify",
                       headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask())


def _review(client, tok, tid):
    return client.post(f"/api/tiles/{tid}/review",
                       headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask(2))


def test_dashboard_reflects_real_actions(client, admin_user, operators, tiles):
    """
    Pre-action: 10 pending. Act: classify 3 (→classified), review 1 (→reviewed),
    report 1 problem (→problem). Post: dashboard counters must match exactly.
    """
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")

    # op1 classifies 3 tiles
    classified = []
    for _ in range(3):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        _classify(client, t1, tile["id"])
        classified.append(tile["id"])

    # op2 reviews 1 of them
    client.get("/api/tiles/next", headers=h(t2))  # assigns first in review queue
    _review(client, t2, classified[0])

    # op1 reports 1 problem on the next pending tile
    problem_tile = client.get("/api/tiles/next", headers=h(t1)).json()
    client.post(f"/api/tiles/{problem_tile['id']}/report-problem",
                headers=h(t1), json={"note": "clouds"})

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    totals = d["totals_by_status"]

    # Expected states after the above:
    #   reviewed  = 1
    #   classified = 2  (3 classified − 1 that got reviewed)
    #   problem   = 1
    #   pending   = 10 − 3 − 1 = 6  (3 became classified, 1 became problem)
    assert totals.get("reviewed") == 1
    assert totals.get("classified") == 2
    assert totals.get("problem") == 1
    assert totals.get("pending") == 6
    assert d["total_tiles"] == 10
    # completion_percent tracks the CLASSIFY step (primary production metric):
    # 2 classified + 1 reviewed (which also passed classify) = 3 of 10.
    assert d["completion_percent"] == 30.0


def test_dashboard_per_operator_counts_match_actions(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")

    # op1: 2 classifications, 1 problem
    for _ in range(2):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        _classify(client, t1, tile["id"])
    bad = client.get("/api/tiles/next", headers=h(t1)).json()
    client.post(f"/api/tiles/{bad['id']}/report-problem",
                headers=h(t1), json={"note": "x"})

    # op2: 1 review
    client.get("/api/tiles/next", headers=h(t2))
    # op2 got one classified by op1 — review it
    # Find which one op2 is assigned to
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM tiles WHERE assigned_to=?",
            (operators[1]["id"],),
        ).fetchone()
    finally:
        conn.close()
    _review(client, t2, row["id"])

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    by_user = {u["username"]: u for u in d["per_operator"]}
    assert by_user["op1"]["classified"] == 2
    assert by_user["op1"]["problems"] == 1
    assert by_user["op2"]["reviewed"] == 1


def test_dashboard_empty_state_no_crash(client, admin_user):
    """Dashboard with zero tiles and zero actions must not divide by zero."""
    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    assert d["total_tiles"] == 0
    assert d["completion_percent"] == 0.0
    assert d["eta_days"] is None
    assert d["avg_classify_seconds"] == 0.0
    assert d["avg_review_seconds"] == 0.0


def test_dashboard_percent_and_rate_track_classification_not_review(
    client, admin_user, operators, tiles
):
    """`completion_percent`, `rate_per_day` and `eta_days` all key off the
    classify step — a tile that is classified but not yet reviewed must
    still count toward completion and throughput. Review is a secondary
    validation step; the team's output metric is classifications done."""
    t1 = token(client, "op1", "secret123")

    # Classify 4 of 10 tiles. Don't review anything.
    for _ in range(4):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        _classify(client, t1, tile["id"])

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()

    assert d["totals_by_status"].get("classified") == 4
    assert d["totals_by_status"].get("reviewed") is None or \
           d["totals_by_status"].get("reviewed") == 0
    # 4 classified / 10 total = 40% — old behavior would have shown 0%.
    assert d["completion_percent"] == 40.0
    # Rate counts classifications in the last 7 days, so 4/7 = ~0.57.
    assert d["rate_per_day"] == round(4 / 7.0, 2)
    # ETA: 6 remaining / 0.57 per day ≈ 10.5 days — must be positive.
    assert d["eta_days"] is not None and d["eta_days"] > 0


def test_dashboard_avg_durations_split_by_action(client, admin_user, operators, tiles):
    """Global and per-operator classify/review times come from action_log
    (assign_* → classify/review pairs) and must be reported separately."""
    import time
    from backend.database import connect, log_action
    from datetime import datetime, timezone, timedelta

    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")

    # op1 classifies two tiles.
    classified_ids = []
    for _ in range(2):
        tile = client.get("/api/tiles/next", headers=h(t1)).json()
        _classify(client, t1, tile["id"])
        classified_ids.append(tile["id"])

    # op2 reviews one of them.
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, classified_ids[0])

    # Backdate assigns so the measured durations are large and deterministic.
    # Rewrite the assign_* rows to 120s before their matching classify/review.
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, action, created_at, user_id, tile_id FROM action_log ORDER BY id"
        ).fetchall()
        for r in rows:
            if r["action"] == "assign_classify":
                finish = conn.execute(
                    "SELECT created_at FROM action_log WHERE action='classify' "
                    "AND user_id=? AND tile_id=? ORDER BY id DESC LIMIT 1",
                    (r["user_id"], r["tile_id"]),
                ).fetchone()
                if finish:
                    new_ts = (datetime.fromisoformat(finish["created_at"])
                              - timedelta(seconds=120)).isoformat()
                    conn.execute("UPDATE action_log SET created_at=? WHERE id=?",
                                 (new_ts, r["id"]))
            elif r["action"] == "assign_review":
                finish = conn.execute(
                    "SELECT created_at FROM action_log WHERE action='review' "
                    "AND user_id=? AND tile_id=? ORDER BY id DESC LIMIT 1",
                    (r["user_id"], r["tile_id"]),
                ).fetchone()
                if finish:
                    new_ts = (datetime.fromisoformat(finish["created_at"])
                              - timedelta(seconds=45)).isoformat()
                    conn.execute("UPDATE action_log SET created_at=? WHERE id=?",
                                 (new_ts, r["id"]))
    finally:
        conn.close()

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()

    # Globals: classify ≈ 120s, review ≈ 45s (±2s slack for julianday rounding)
    assert 118 <= d["avg_classify_seconds"] <= 122
    assert 43 <= d["avg_review_seconds"] <= 47

    by_user = {u["username"]: u for u in d["per_operator"]}
    assert 118 <= by_user["op1"]["avg_classify_seconds"] <= 122
    assert by_user["op1"]["avg_review_seconds"] == 0.0
    assert by_user["op2"]["avg_classify_seconds"] == 0.0
    assert 43 <= by_user["op2"]["avg_review_seconds"] <= 47


# ---------- Pause subtraction ----------

def _set_log_ts(conn, log_id, ts):
    conn.execute("UPDATE action_log SET created_at=? WHERE id=?", (ts, log_id))


def _log_id(conn, user_id, tile_id, action):
    row = conn.execute(
        "SELECT id FROM action_log WHERE user_id=? AND tile_id=? AND action=? ORDER BY id DESC LIMIT 1",
        (user_id, tile_id, action),
    ).fetchone()
    return row["id"] if row else None


def test_dashboard_subtracts_pause_intervals(client, admin_user, operators, tiles):
    """Pause→resume intervals inside an assign→classify cycle must be
    subtracted from the measured classify duration. Otherwise an operator
    that pauses overnight would inflate the avg by 12+ hours."""
    from datetime import datetime, timedelta, timezone
    from backend.database import connect, log_action

    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    # Insert pause/resume log entries before classifying
    conn = connect()
    try:
        log_action(conn, operators[0]["id"], tile["id"], "pause")
        log_action(conn, operators[0]["id"], tile["id"], "resume")
    finally:
        conn.close()
    _classify(client, t1, tile["id"])

    # Backdate so total span = 80min, pause interval = 60min → net = 20min
    base = datetime.now(timezone.utc)
    conn = connect()
    try:
        assign_id = _log_id(conn, operators[0]["id"], tile["id"], "assign_classify")
        pause_id  = _log_id(conn, operators[0]["id"], tile["id"], "pause")
        resume_id = _log_id(conn, operators[0]["id"], tile["id"], "resume")
        classify_id = _log_id(conn, operators[0]["id"], tile["id"], "classify")
        _set_log_ts(conn, assign_id,   (base - timedelta(minutes=80)).isoformat())
        _set_log_ts(conn, pause_id,    (base - timedelta(minutes=70)).isoformat())
        _set_log_ts(conn, resume_id,   (base - timedelta(minutes=10)).isoformat())
        _set_log_ts(conn, classify_id,  base.isoformat())
    finally:
        conn.close()

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    # Expect ~20 min = 1200s (±2s slack for julianday rounding)
    assert 1198 <= d["avg_classify_seconds"] <= 1202, d["avg_classify_seconds"]


def test_dashboard_handles_orphan_pause(client, admin_user, operators, tiles):
    """Pause without a matching resume (operator never returned) must not
    break the SQL nor subtract any time. Cycle without classify is ignored."""
    from datetime import datetime, timedelta, timezone
    from backend.database import connect, log_action

    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    # Pause but never resume nor classify — cycle is incomplete, dashboard ignores it.
    conn = connect()
    try:
        log_action(conn, operators[0]["id"], tile["id"], "pause")
    finally:
        conn.close()

    # Now classify a SECOND tile normally so there IS a measurable cycle.
    tile2 = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile2["id"])

    base = datetime.now(timezone.utc)
    conn = connect()
    try:
        a2 = _log_id(conn, operators[0]["id"], tile2["id"], "assign_classify")
        c2 = _log_id(conn, operators[0]["id"], tile2["id"], "classify")
        _set_log_ts(conn, a2, (base - timedelta(seconds=100)).isoformat())
        _set_log_ts(conn, c2, base.isoformat())
    finally:
        conn.close()

    adm = token(client, "admin", "admin123")
    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    # Only the second tile contributes; orphan pause didn't crash anything.
    assert 98 <= d["avg_classify_seconds"] <= 102


def test_dashboard_pause_scoped_to_latest_cycle(client, admin_user, operators, tiles):
    """Pauses from a previous (reset, re-assigned) cycle must NOT be subtracted
    from a fresh assign→classify cycle."""
    from datetime import datetime, timedelta, timezone
    from backend.database import connect, log_action

    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    # First cycle: pause+resume+classify (we'll wipe via admin reset).
    conn = connect()
    try:
        log_action(conn, operators[0]["id"], tile["id"], "pause")
        log_action(conn, operators[0]["id"], tile["id"], "resume")
    finally:
        conn.close()
    _classify(client, t1, tile["id"])

    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))

    # Second cycle: same operator, same tile, no pauses this time.
    tile_again = client.get("/api/tiles/next", headers=h(t1)).json()
    assert tile_again["id"] == tile["id"]
    _classify(client, t1, tile_again["id"])

    # Backdate so the OLD cycle had a 60-min pause and the NEW cycle is 100s flat.
    base = datetime.now(timezone.utc)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, action FROM action_log WHERE tile_id=? AND user_id=? ORDER BY id",
            (tile["id"], operators[0]["id"]),
        ).fetchall()
        # rows order: assign_classify, pause, resume, classify, reset(?), assign_classify, classify
        # reset is logged under admin_id, so it doesn't appear here.
        # Old cycle backdating
        _set_log_ts(conn, rows[0]["id"], (base - timedelta(hours=24, minutes=80)).isoformat())
        _set_log_ts(conn, rows[1]["id"], (base - timedelta(hours=24, minutes=70)).isoformat())
        _set_log_ts(conn, rows[2]["id"], (base - timedelta(hours=24, minutes=10)).isoformat())
        _set_log_ts(conn, rows[3]["id"], (base - timedelta(hours=24)).isoformat())
        # New cycle backdating: 100s span, no pause
        _set_log_ts(conn, rows[4]["id"], (base - timedelta(seconds=100)).isoformat())
        _set_log_ts(conn, rows[5]["id"], base.isoformat())
    finally:
        conn.close()

    d = client.get("/api/admin/dashboard", headers=h(adm)).json()
    # Latest cycle wins (MAX(assign_classify), MAX(classify)) → 100s, with no
    # pause to subtract (the old pause is OUTSIDE the new cycle).
    assert 98 <= d["avg_classify_seconds"] <= 102, d["avg_classify_seconds"]
