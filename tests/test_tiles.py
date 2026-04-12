import numpy as np
from tests.conftest import token


def headers(t): return {"Authorization": f"Bearer {t}"}


def test_next_assigns_and_resumes(client, operators, tiles):
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/next", headers=headers(t))
    assert r.status_code == 200
    first = r.json()
    assert first["status"] == "in_progress"
    assert first["assigned_to"] == operators[0]["id"]

    # Second call returns the SAME tile (resume)
    r2 = client.get("/api/tiles/next", headers=headers(t))
    assert r2.status_code == 200
    assert r2.json()["id"] == first["id"]


def test_next_different_operators_get_different_tiles(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    t2 = token(client, "op2", "secret123")
    r1 = client.get("/api/tiles/next", headers=headers(t1)).json()
    r2 = client.get("/api/tiles/next", headers=headers(t2)).json()
    assert r1["id"] != r2["id"]


def test_preview_does_not_assign(client, operators, tiles):
    t = token(client, "op1", "secret123")
    r = client.get("/api/tiles/next-preview", headers=headers(t))
    assert r.status_code == 200
    peek = r.json()
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status, assigned_to FROM tiles WHERE id=?", (peek["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["assigned_to"] is None


def test_classify_submit_valid(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(t)).json()
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**headers(t), "Content-Type": "application/octet-stream"},
                    content=raw)
    assert r.status_code == 200
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status, classified_by FROM tiles WHERE id=?", (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["classified_by"] == operators[0]["id"]


def test_classify_rejects_unfilled(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(t)).json()
    raw = bytearray(65536)
    for i in range(65536): raw[i] = 1
    raw[0] = 255  # one unfilled
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**headers(t), "Content-Type": "application/octet-stream"},
                    content=bytes(raw))
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["error"] == "unfilled_pixels"
    assert body["detail"]["missing"] == 1


def test_classify_rejects_wrong_size(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**headers(t), "Content-Type": "application/octet-stream"},
                    content=b"\x01" * 100)
    assert r.status_code in (400, 422, 500)  # ValueError propagates


def test_reviewer_never_gets_own_classification(client, operators, tiles):
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(t1)).json()
    raw = np.full(65536, 1, dtype=np.uint8).tobytes()
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**headers(t1), "Content-Type": "application/octet-stream"},
                content=raw)
    # op1 asks for next — should NOT be given the tile they classified
    nxt = client.get("/api/tiles/next", headers=headers(t1)).json()
    assert nxt["id"] != tile["id"]

    # op2 should be given that tile for review (priority)
    t2 = token(client, "op2", "secret123")
    nxt2 = client.get("/api/tiles/next", headers=headers(t2)).json()
    assert nxt2["id"] == tile["id"]
    assert nxt2["status"] == "in_review"


def test_report_problem(client, operators, tiles):
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=headers(t)).json()
    r = client.post(f"/api/tiles/{tile['id']}/report-problem",
                    headers=headers(t), json={"note": "nuvens cobrem a área"})
    assert r.status_code == 200
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status, problem_note FROM tiles WHERE id=?", (tile["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "problem"
    assert "nuvens" in row["problem_note"]
