"""Concurrency beyond the basic pending-queue race: reviewers, admin actions."""
import threading
import time
import numpy as np
import pytest
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _mask(fill=1):
    return np.full(65536, fill, dtype=np.uint8).tobytes()


def test_10_reviewers_race_on_classified_queue(client, operators_10, tiles_many):
    """
    Populate 20 tiles in status=classified (by op1). Launch 10 reviewer threads
    (op2..op11, none of them the classifier) hitting /next simultaneously.
    Invariant: each reviewer gets a distinct tile, none assigned to its classifier,
    no duplicate assignments, none of them can be the classifier.
    """
    from backend.database import connect, transaction
    from backend.mask_utils import empty_mask_png

    classifier_id = operators_10[0]["id"]
    empty = empty_mask_png()

    # Seed: force 20 tiles into classified by op1
    with transaction("IMMEDIATE") as conn:
        rows = conn.execute(
            "SELECT id FROM tiles WHERE status='pending' ORDER BY id LIMIT 20"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE tiles SET status='classified', classified_by=?, "
                "classified_at=datetime('now'), data_png=? WHERE id=?",
                (classifier_id, empty, r["id"]),
            )

    # Log in the 9 reviewers (op2..op10). op1 is the classifier — excluded.
    from backend import auth as authmod
    tokens = []
    for i in range(2, 11):
        authmod.reset_rate_limits()
        tokens.append(token(client, f"op{i}", "secret123"))

    results = [None] * len(tokens)
    barrier = threading.Barrier(len(tokens))

    def worker(idx):
        barrier.wait()
        r = client.get("/api/tiles/next", headers=h(tokens[idx]))
        results[idx] = (r.status_code, r.json() if r.status_code == 200 else None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(tokens))]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"{elapsed:.2f}s — race took too long"

    # Assertions
    tile_ids = [res[1]["id"] for res in results if res[1]]
    assert len(tile_ids) == len(set(tile_ids)), (
        f"Duplicate tile assignment: {sorted(tile_ids)}"
    )

    # No reviewer is the classifier (all reviewers are op2..op10, classifier=op1)
    for status, body in results:
        assert status == 200
        assert body["status"] == "in_review"
        assert body["classified_by"] == classifier_id
        assert body["assigned_to"] != classifier_id


def test_classify_and_admin_reset_race(client, admin_user, operators_10, tiles):
    """Op1 classifies while admin resets the same tile concurrently.
    Final state must be consistent (not a mix of statuses).
    """
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    adm = token(client, "admin", "admin123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    results = {}
    barrier = threading.Barrier(2)  # release both writers at the same instant
    def do_classify():
        barrier.wait()
        r = client.post(f"/api/tiles/{tile['id']}/classify",
                        headers={**h(t1), "Content-Type": "application/octet-stream"},
                        content=_mask(1))
        results["classify"] = r.status_code

    def do_reset():
        barrier.wait()
        r = client.post(f"/api/admin/tiles/{tile['id']}/reset", headers=h(adm))
        results["reset"] = r.status_code

    threads = [threading.Thread(target=do_classify), threading.Thread(target=do_reset)]
    for t in threads: t.start()
    for t in threads: t.join()

    # Whoever won, final state is consistent.
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, classified_by, assigned_to FROM tiles WHERE id=?",
            (tile["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] in ("pending", "classified")
    if row["status"] == "pending":
        assert row["classified_by"] is None
        assert row["assigned_to"] is None
    else:
        assert row["classified_by"] is not None


def test_admin_assign_during_classify_keeps_state_coherent(
    client, admin_user, operators, tiles
):
    """Admin tenta atribuir reviewer ao mesmo tile que op1 está submetendo.
    O backend usa BEGIN IMMEDIATE em ambos os caminhos, então um deve serializar
    atrás do outro. Aceitamos os dois resultados, desde que o estado final seja
    coerente — não pode ficar com `status=classified` e `assigned_to` apontando
    pra alguém que não classificou (esse seria o failure mode real)."""
    from backend.database import connect
    t1 = token(client, "op1", "secret123")
    adm = token(client, "admin", "admin123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()
    op1_id = tile["assigned_to"]
    op2_id = next(o["id"] for o in (
        client.get("/api/admin/users", headers=h(adm)).json()
    ) if o["username"] == "op2")

    barrier = threading.Barrier(2)
    results = {}

    def do_classify():
        barrier.wait()
        r = client.post(
            f"/api/tiles/{tile['id']}/classify",
            headers={**h(t1), "Content-Type": "application/octet-stream"},
            content=_mask(2),
        )
        results["classify"] = r.status_code

    def do_assign():
        barrier.wait()
        r = client.post(
            "/api/admin/tiles/assign",
            headers=h(adm),
            json={"tile_ids": [tile["id"]], "user_id": op2_id},
        )
        results["assign"] = r.status_code

    threads = [threading.Thread(target=do_classify), threading.Thread(target=do_assign)]
    for t in threads: t.start()
    for t in threads: t.join()

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, classified_by, assigned_to, paused_at "
            "FROM tiles WHERE id=?", (tile["id"],),
        ).fetchone()
    finally:
        conn.close()

    # Three coherent outcomes are possible depending on which transaction
    # serialized first:
    #   (a) classify wins, assign aborts on the gate     → status=classified, no review queued
    #   (b) classify wins, assign queues op2 for review  → status=in_review,  assigned_to=op2 (queue-paused)
    #   (c) assign wins, classify hits stale assignment   → status=in_progress|in_review, classify=403
    # The forbidden outcome is "classified_by != op1" or "assigned_to=op1
    # while op2 just got assigned" — we assert against those.
    final = row["status"]
    if results["classify"] == 200:
        # op1 owns the classification regardless of what assign did afterwards.
        assert row["classified_by"] == op1_id, "classify succeeded but classifier wrong"
        assert final in ("classified", "in_review"), final
        if final == "in_review":
            # Assign came after and queued op2 for review.
            assert row["assigned_to"] == op2_id
            assert row["paused_at"] is not None
            assert results["assign"] == 200
    else:
        # Classify lost — either status moved out from under it (assign won) or
        # version/state guard rejected. Either way op1 cannot be the classifier.
        assert results["classify"] in (403, 409), results["classify"]
        assert row["classified_by"] is None
        assert final in ("in_progress", "in_review", "classified"), final


def test_same_refresh_token_used_concurrently(client, admin_user):
    """Multiple concurrent refresh with same refresh_token — backend must not crash,
    rotating or not. Simply asserts no 5xx and all responses are coherent."""
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    refresh_tok = r.json()["refresh_token"]

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        r = client.post("/api/auth/refresh", json={"refresh_token": refresh_tok})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert all(s < 500 for s in results), f"5xx on concurrent refresh: {results}"
    # Today we don't rotate → all 5 should succeed. If rotation is introduced later,
    # this assertion documents the new expectation.
    assert all(s == 200 for s in results), f"Unexpected non-200: {results}"
