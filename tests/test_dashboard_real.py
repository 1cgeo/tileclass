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
    assert d["completion_percent"] == 10.0  # 1 reviewed / 10 total


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
