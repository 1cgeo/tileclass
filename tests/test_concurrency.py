"""Critical test: /api/tiles/next must never assign the same tile twice under concurrent access."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from tests.conftest import token


def _race_next(tokens):
    """Fire one /next per token with all threads released simultaneously via a
    Barrier. Without the barrier the threadpool can serialize calls and the
    assign-race never actually happens (project test rubric)."""
    from backend.main import app
    barrier = threading.Barrier(len(tokens))
    results = []
    lock = threading.Lock()

    def hit(tok):
        with TestClient(app) as c:
            barrier.wait()  # all threads hit the atomic SELECT...UPDATE at once
            r = c.get("/api/tiles/next", headers={"Authorization": f"Bearer {tok}"})
            with lock:
                results.append(r.json())

    threads = [threading.Thread(target=hit, args=(t,)) for t in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_next_no_duplicates(client, operators, tiles):
    tokens = [token(client, op["username"], op["password"]) for op in operators]
    results = _race_next(tokens)
    tile_ids = [r["id"] for r in results]
    # Every operator got a tile, and no two operators got the SAME tile.
    assert len(tile_ids) == len(tokens)
    assert all(isinstance(i, int) for i in tile_ids)
    assert len(set(tile_ids)) == len(tile_ids), f"duplicates: {tile_ids}"


def test_concurrent_10_operators_no_duplicates(client, operators_10, tiles_many):
    """11.7 acceptance: 10 simultaneous operators without contention-induced duplicates."""
    from backend.auth import reset_rate_limits
    tokens = []
    for op in operators_10:
        reset_rate_limits()  # rate limit is per-IP (127.0.0.1 in tests)
        tokens.append(token(client, op["username"], op["password"]))

    results = _race_next(tokens)
    tile_ids = [r["id"] for r in results]
    assert len(set(tile_ids)) == 10, f"duplicates among 10 ops: {tile_ids}"
    # Each assignment is reflected in the DB exactly once (no double-assign that
    # the response JSON might have hidden).
    from backend.database import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, assigned_to, status FROM tiles WHERE id IN (%s)"
            % ",".join("?" * len(tile_ids)), tile_ids,
        ).fetchall()
    finally:
        conn.close()
    assert all(r["status"] == "in_progress" and r["assigned_to"] is not None for r in rows)
    assert len({r["assigned_to"] for r in rows}) == 10  # 10 distinct operators


def test_next_tile_latency_under_2s(client, operators, tiles):
    """11.7 acceptance: single /next must complete in < 2s."""
    tok = token(client, "op1", "secret123")
    t0 = time.time()
    r = client.get("/api/tiles/next", headers={"Authorization": f"Bearer {tok}"})
    elapsed = time.time() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, f"/next took {elapsed:.3f}s"


def test_same_user_many_calls_returns_same_tile(client, operators, tiles):
    from backend.main import app
    tok = token(client, "op1", "secret123")

    def hit(_):
        with TestClient(app) as c:
            return c.get("/api/tiles/next", headers={"Authorization": f"Bearer {tok}"}).json()

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(hit, range(10)))

    ids = set(r["id"] for r in results)
    # Same user hammering /next should always resume to the same tile
    assert len(ids) == 1
