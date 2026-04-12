"""Critical test: /api/tiles/next must never assign the same tile twice under concurrent access."""
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from tests.conftest import token


def test_concurrent_next_no_duplicates(client, operators, tiles):
    from backend.main import app
    tokens = [token(client, op["username"], op["password"]) for op in operators]

    # Spawn N threads, each hitting /next once. With 3 ops and 10 tiles, each op should get a unique tile.
    results = []
    def hit(tok):
        with TestClient(app) as c:
            r = c.get("/api/tiles/next", headers={"Authorization": f"Bearer {tok}"})
            return r.json()

    with ThreadPoolExecutor(max_workers=len(tokens)) as ex:
        for r in ex.map(hit, tokens):
            results.append(r)

    tile_ids = [r["id"] for r in results]
    assert len(set(tile_ids)) == len(tile_ids), f"duplicates: {tile_ids}"


def test_concurrent_10_operators_no_duplicates(client, operators_10, tiles_many):
    """11.7 acceptance: 10 simultaneous operators without contention-induced duplicates."""
    from backend.main import app
    from backend.auth import reset_rate_limits
    tokens = []
    for op in operators_10:
        reset_rate_limits()  # rate limit is per-IP (127.0.0.1 in tests)
        tokens.append(token(client, op["username"], op["password"]))

    def hit(tok):
        with TestClient(app) as c:
            r = c.get("/api/tiles/next", headers={"Authorization": f"Bearer {tok}"})
            return r.json(), r.elapsed.total_seconds() if hasattr(r, "elapsed") else 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        pairs = list(ex.map(hit, tokens))
    elapsed = time.time() - t0

    results = [p[0] for p in pairs]
    tile_ids = [r["id"] for r in results]
    assert len(set(tile_ids)) == 10, f"duplicates among 10 ops: {tile_ids}"
    # 11.7: loading next tile < 2s
    assert elapsed < 2.0 * 10, f"10 /next took {elapsed:.2f}s (avg {elapsed/10:.2f}s)"


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
