"""Complete tile state machine coverage.

Transitions from CLAUDE.md:
  pending → in_progress → classified → in_review → reviewed
  any → problem
  problem → pending (admin reset)
  reviewed → in_review/classified (admin re-review)
"""
import numpy as np
import pytest
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def _classify(client, tok, tid, fill=1):
    return client.post(f"/api/tiles/{tid}/classify",
                       headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask(fill))


def _review(client, tok, tid, fill=2):
    return client.post(f"/api/tiles/{tid}/review",
                       headers={**h(tok), "Content-Type": "application/octet-stream"},
                       content=_mask(fill))


def _status(client, tok, tid):
    return client.get(f"/api/tiles/{tid}", headers=h(tok)).json()["status"]


# ---------- Forward transitions ----------

def test_pending_to_in_progress_via_next(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    assert tile["status"] == "in_progress"


def test_in_progress_to_classified_via_classify(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    assert _classify(client, t, tile["id"]).status_code == 200
    assert _status(client, t, tile["id"]) == "classified"


def test_classified_to_in_review_via_next_by_other(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t2)).json()
    assert nxt["id"] == tile["id"]
    assert nxt["status"] == "in_review"


def test_in_review_to_reviewed_via_review(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    assert _review(client, t2, tile["id"]).status_code == 200
    assert _status(client, t2, tile["id"]) == "reviewed"


# ---------- Problem branch ----------

def test_in_progress_to_problem(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=h(t), json={"note": "clouds"})
    assert r.status_code == 200
    assert _status(client, t, tile["id"]) == "problem"


def test_problem_tile_excluded_from_next(client, operators, tiles):
    """Problem tile must never be returned by /next (neither to classifier nor reviewer)."""
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    client.post(f"/api/tiles/{tile['id']}/report-problem",
                headers=h(t1), json={"note": "bad"})

    # op2 should skip the problem tile and get a pending one
    t2 = token(client, "op2", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t2)).json()
    assert nxt["id"] != tile["id"]
    assert nxt["status"] == "in_progress"


def test_problem_to_pending_via_admin_reset(client, admin_user, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    client.post(f"/api/tiles/{tile['id']}/report-problem",
                headers=h(t), json={"note": "bad"})
    assert _status(client, t, tile["id"]) == "problem"

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))
    assert r.status_code == 200
    assert _status(client, adm, tile["id"]) == "pending"

    # Requeuable and FIFO: the reset tile was the first one assigned, so it has
    # the smallest id among pending tiles and /next returns it first.
    t2 = token(client, "op2", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t2))
    assert nxt.status_code == 200
    assert nxt.json()["id"] == tile["id"]
    assert nxt.json()["status"] == "in_progress"


def test_reviewed_to_classified_via_admin_rereview(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])
    assert _status(client, t2, tile["id"]) == "reviewed"

    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/re-review", headers=h(adm))
    assert r.status_code == 200
    assert _status(client, adm, tile["id"]) == "classified"


# ---------- Illegal transitions ----------

@pytest.mark.parametrize("initial_status",
                         ["reviewed", "pending", "classified", "blocked", "problem"])
def test_classify_rejected_in_wrong_state(client, operators, tiles, initial_status):
    """The submit endpoint acts only from in_progress (→classified) or in_review
    (→reviewed). Every other state is rejected 409 and left untouched. (in_review
    is intentionally absent — there the same endpoint legally performs a review.)"""
    from backend.database import connect, transaction

    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET status=?, assigned_to=? WHERE id=?",
            (initial_status, operators[0]["id"], tile["id"]),
        )

    r = _classify(client, t, tile["id"])
    assert r.status_code == 409, f"expected 409 from {initial_status}, got {r.status_code}"
    # State untouched: still in the forced status, not classified.
    conn = connect()
    try:
        row = conn.execute("SELECT status, classified_by FROM tiles WHERE id=?",
                           (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == initial_status and row["classified_by"] is None


def test_rereview_on_non_reviewed_tile_is_rejected(client, admin_user, operators, tiles):
    """re-review on a tile that is not reviewed (e.g. pending) → 409."""
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM tiles WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{row['id']}/re-review", headers=h(adm))
    assert r.status_code == 409


def test_pause_does_not_change_status(client, operators, tiles):
    """Pause is orthogonal to the state machine: status stays in_progress/in_review."""
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    body = _mask(fill=255)  # totally empty mask is allowed by pause
    r = client.post(f"/api/tiles/{tile['id']}/pause",
                    headers={**h(t), "Content-Type": "application/octet-stream",
                             "X-Tile-Version": str(tile["version"])},
                    content=body)
    assert r.status_code == 200
    assert _status(client, t, tile["id"]) == "in_progress"
    # Resume also keeps status the same
    r = client.post(f"/api/tiles/{tile['id']}/resume", headers=h(t))
    assert r.status_code == 200
    assert _status(client, t, tile["id"]) == "in_progress"


def test_reset_of_reviewed_tile_wipes_all_user_fks(client, admin_user, operators, tiles):
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])

    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, classified_by, reviewed_by, classified_at, reviewed_at, "
            "assigned_to, problem_note FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["classified_by"] is None
    assert row["reviewed_by"] is None
    assert row["classified_at"] is None
    assert row["reviewed_at"] is None
    assert row["assigned_to"] is None
    assert row["problem_note"] is None
