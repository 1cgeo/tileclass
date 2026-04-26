"""Admin block/unblock: gates, state restoration, queue exclusion."""
import threading
import numpy as np
import pytest
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def _classify(client, tok, tid, fill=1):
    return client.post(
        f"/api/tiles/{tid}/classify",
        headers={**h(tok), "Content-Type": "application/octet-stream"},
        content=_mask(fill),
    )


def _review(client, tok, tid, fill=2):
    return client.post(
        f"/api/tiles/{tid}/review",
        headers={**h(tok), "Content-Type": "application/octet-stream"},
        content=_mask(fill),
    )


def _status(client, tok, tid):
    return client.get(f"/api/tiles/{tid}", headers=h(tok)).json()["status"]


def _blocked_from(tid):
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT blocked_from FROM tiles WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    return row["blocked_from"] if row else None


# ---------- Happy path ----------

def test_block_pending_tile(client, admin_user, tiles):
    """Bloquear um tile pending: status, blocked_from, action_log e reason
    persistidos numa única transação (verificação direta no DB para travar
    contra regressões silenciosas — só checar HTTP 200 mascarava commits parciais)."""
    from backend.database import connect
    adm = token(client, "admin", "admin123")
    tid = 1
    r = client.post(f"/api/admin/tiles/{tid}/block",
                    headers=h(adm), json={"reason": "spurious"})
    assert r.status_code == 200
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, blocked_from, version FROM tiles WHERE id=?", (tid,)
        ).fetchone()
        log = conn.execute(
            "SELECT action, detail FROM action_log WHERE tile_id=? AND action='block'",
            (tid,),
        ).fetchall()
    finally:
        conn.close()
    assert row["status"] == "blocked"
    assert row["blocked_from"] == "pending"
    assert row["version"] >= 2  # bumped at least once vs initial version=1
    assert len(log) == 1, f"expected 1 'block' log entry, got {len(log)}"
    # The reason flows through to action_log.detail (used by admin audit trail).
    assert log[0]["detail"] is not None and "spurious" in log[0]["detail"]


def test_block_classified_tile(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/block", headers=h(adm))
    assert r.status_code == 200
    assert _status(client, adm, tile["id"]) == "blocked"
    assert _blocked_from(tile["id"]) == "classified"


def test_block_reviewed_tile(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))
    _review(client, t2, tile["id"])
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/block", headers=h(adm))
    assert r.status_code == 200
    assert _blocked_from(tile["id"]) == "reviewed"


# ---------- Gate: cannot block in-execution or problem ----------

def test_block_in_progress_rejected(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    assert tile["status"] == "in_progress"
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/block", headers=h(adm))
    assert r.status_code == 409
    # Tile still in the same state, no blocked_from set
    assert _status(client, adm, tile["id"]) == "in_progress"
    assert _blocked_from(tile["id"]) is None


def test_block_in_review_rejected(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    t2 = token(client, "op2", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t2)).json()
    assert nxt["status"] == "in_review"
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{nxt['id']}/block", headers=h(adm))
    assert r.status_code == 409


def test_block_problem_rejected(client, admin_user, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()
    client.post(f"/api/tiles/{tile['id']}/report-problem",
                headers=h(t), json={"note": "clouds"})
    adm = token(client, "admin", "admin123")
    r = client.post(f"/api/admin/tiles/{tile['id']}/block", headers=h(adm))
    assert r.status_code == 409


def test_block_already_blocked_rejected(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    tid = 1
    client.post(f"/api/admin/tiles/{tid}/block", headers=h(adm))
    r = client.post(f"/api/admin/tiles/{tid}/block", headers=h(adm))
    assert r.status_code == 409


# ---------- Authorization ----------

def test_block_requires_admin(client, operators, tiles):
    t = token(client, "op1", "secret123")
    r = client.post("/api/admin/tiles/1/block", headers=h(t))
    assert r.status_code == 403


# ---------- Unblock ----------

def test_unblock_restores_pending(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    client.post("/api/admin/tiles/1/block", headers=h(adm))
    r = client.post("/api/admin/tiles/1/unblock", headers=h(adm))
    assert r.status_code == 200
    assert _status(client, adm, 1) == "pending"
    assert _blocked_from(1) is None


def test_unblock_restores_classified(client, admin_user, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile["id"])
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile['id']}/block", headers=h(adm))
    client.post(f"/api/admin/tiles/{tile['id']}/unblock", headers=h(adm))
    assert _status(client, adm, tile["id"]) == "classified"


def test_unblock_non_blocked_rejected(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    r = client.post("/api/admin/tiles/1/unblock", headers=h(adm))
    assert r.status_code == 409


# ---------- Queue exclusion (the whole point) ----------

def test_blocked_tile_not_distributed_as_classify(client, admin_user, operators, tiles):
    """Blocked pending tile must be skipped by /api/tiles/next."""
    adm = token(client, "admin", "admin123")
    # Block tiles 1..5; queue should hand out tile 6 first.
    for tid in range(1, 6):
        client.post(f"/api/admin/tiles/{tid}/block", headers=h(adm))
    t = token(client, "op1", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t)).json()
    assert nxt["id"] == 6
    assert nxt["status"] == "in_progress"


def test_blocked_tile_not_distributed_as_review(client, admin_user, operators, tiles):
    """Blocked classified tile must be skipped by the review queue."""
    t1 = token(client, "op1", "secret123")
    tile_a = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile_a["id"])
    tile_b = client.get("/api/tiles/next", headers=h(t1)).json()
    _classify(client, t1, tile_b["id"])
    adm = token(client, "admin", "admin123")
    client.post(f"/api/admin/tiles/{tile_a['id']}/block", headers=h(adm))
    t2 = token(client, "op2", "secret123")
    nxt = client.get("/api/tiles/next", headers=h(t2)).json()
    # op2 receives tile_b for review, never tile_a (blocked).
    assert nxt["id"] == tile_b["id"]
    assert nxt["status"] == "in_review"


def test_blocked_tile_not_in_preview(client, admin_user, operators, tiles):
    adm = token(client, "admin", "admin123")
    client.post("/api/admin/tiles/1/block", headers=h(adm))
    # peek_next_tile falls through to the pending queue; with tile 1 blocked,
    # the operator's preview should be the next non-blocked id.
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/next-preview", headers=h(t))
    assert r.status_code == 200
    assert r.json()["id"] != 1


def test_blocked_tile_cannot_be_admin_assigned(client, admin_user, operators, tiles):
    """admin_service.assign_many rejects non-(pending|classified); blocked is rejected."""
    adm = token(client, "admin", "admin123")
    client.post("/api/admin/tiles/1/block", headers=h(adm))
    r = client.post(
        "/api/admin/tiles/assign",
        headers=h(adm),
        json={"tile_ids": [1], "user_id": operators[0]["id"]},
    )
    assert r.status_code == 409


# ---------- Bulk ----------

def test_bulk_block_atomic(client, admin_user, operators, tiles):
    """If any tile in the batch fails the gate, nothing is written."""
    t1 = token(client, "op1", "secret123")
    bad = client.get("/api/tiles/next", headers=h(t1)).json()  # in_progress
    adm = token(client, "admin", "admin123")
    other_ids = [tid for tid in range(1, 6) if tid != bad["id"]]
    r = client.post(
        "/api/admin/tiles/bulk/block",
        headers=h(adm),
        json={"ids": other_ids + [bad["id"]]},
    )
    assert r.status_code == 409
    # None of the good tiles should be blocked either.
    for tid in other_ids:
        assert _status(client, adm, tid) == "pending"


def test_bulk_block_happy(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    r = client.post(
        "/api/admin/tiles/bulk/block",
        headers=h(adm),
        json={"ids": [1, 2, 3], "reason": "area fora do escopo"},
    )
    assert r.status_code == 200
    assert r.json()["affected"] == 3
    for tid in (1, 2, 3):
        assert _status(client, adm, tid) == "blocked"


def test_bulk_unblock_happy(client, admin_user, tiles):
    adm = token(client, "admin", "admin123")
    client.post("/api/admin/tiles/bulk/block", headers=h(adm), json={"ids": [1, 2, 3]})
    r = client.post("/api/admin/tiles/bulk/unblock", headers=h(adm), json={"ids": [1, 2, 3]})
    assert r.status_code == 200
    for tid in (1, 2, 3):
        assert _status(client, adm, tid) == "pending"


# ---------- Concurrency: blocked tile never leaks to a concurrent operator ----------

def test_concurrent_operators_skip_blocked_head(client, admin_user, operators_10, tiles_many):
    """Block tiles 1..10, then fire 10 operators at /next. No operator gets a
    blocked tile. The race matters because /next is transactional and the block
    status is evaluated inside that transaction."""
    from backend import auth
    adm = token(client, "admin", "admin123")
    blocked_ids = list(range(1, 11))
    for tid in blocked_ids:
        client.post(f"/api/admin/tiles/{tid}/block", headers=h(adm))

    # 10 logins on the same IP blow past the 5/min rate limit.
    tokens = []
    for i in range(10):
        auth.reset_rate_limits()
        tokens.append(token(client, f"op{i+1}", "secret123"))
    results = [None] * 10
    start = threading.Barrier(10)

    def worker(i):
        start.wait()
        r = client.get("/api/tiles/next", headers=h(tokens[i]))
        results[i] = r.json() if r.status_code == 200 else None

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    for t in results:
        assert t is not None
        assert t["id"] not in blocked_ids
