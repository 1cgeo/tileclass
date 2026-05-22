"""Class-distribution panel: per-class pixel totals across submitted tiles.

Cached on submit via `tiles.class_counts`; aggregated by the dashboard
endpoint without re-decoding masks. Legacy tiles are backfilled by
`scripts/recompute_class_counts.py`.
"""
import json
import numpy as np
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _classify(client, op_tok, class_id: int) -> int:
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    raw = bytes([class_id]) * 65536
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert r.status_code == 200, r.text
    return nxt["id"]


def test_class_distribution_pct_is_per_project(client, admin_user, tiles):
    """Percentages use a PER-PROJECT denominator — a class's pct is its share of
    its own project, not of the global pixel total across all projects."""
    from backend.database import connect
    from backend.admin.dashboard import class_distribution
    conn = connect()
    try:
        # Project 1 (default): one fully-class-1 tile.
        conn.execute("UPDATE tiles SET status='reviewed', class_counts=? WHERE id=1",
                     (json.dumps({"1": 65536}),))
        # Project 2 (raster) with two classes; tiles split 30000/10000 px.
        conn.execute("INSERT INTO projects(id,name,kind,tile_px,meters_per_pixel,"
                     "mask_complete_required,primary_mbtiles,active,created_at) "
                     "VALUES (2,'p2','raster',256,2.5,1,'',1,'2026-01-01T00:00:00+00:00')")
        for cid, color in ((2, "#111111"), (3, "#222222")):
            conn.execute("INSERT INTO project_classes(project_id,class_id,name,color,ordering) "
                         "VALUES (2,?,?,?,?)", (cid, f"c{cid}", color, cid))
        for nm, cc in (("p2a", {"2": 30000}), ("p2b", {"3": 10000})):
            conn.execute("INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
                         "status,class_counts) VALUES (2,?,0,0,0.1,0.1,'reviewed',?)",
                         (nm, json.dumps(cc)))
        conn.commit()
    finally:
        conn.close()
    dist = class_distribution()
    by = {(d["project_id"], d["class_id"]): d["pct"] for d in dist}
    assert by[(1, 1)] == 100.0          # project 1: class 1 is 100% of its 65536
    assert by[(2, 2)] == 75.0           # project 2 total 40000 → 30000/40000
    assert by[(2, 3)] == 25.0           # 10000/40000 (NOT diluted by project 1)


def test_mixed_class_counts_split_correctly(client, admin_user, operators, tiles):
    """A non-uniform mask must record per-class pixel counts that sum to the
    tile area — guards against a transpose/aggregation bug that a uniform-fill
    test (every other test here) would never catch."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    raw = bytearray(65536)
    for i in range(65536):
        raw[i] = 1 if i < 20000 else (2 if i < 50000 else 3)  # 20000 / 30000 / 15536
    r = client.post(f"/api/tiles/{nxt['id']}/classify",
                    headers={**h(op_tok), "Content-Type": "application/octet-stream"},
                    content=bytes(raw))
    assert r.status_code == 200, r.text
    from backend.database import connect
    conn = connect()
    try:
        cc = json.loads(conn.execute("SELECT class_counts FROM tiles WHERE id=?", (nxt["id"],)).fetchone()["class_counts"])
    finally:
        conn.close()
    assert cc == {"1": 20000, "2": 30000, "3": 15536}
    assert sum(cc.values()) == 65536
    # And the dashboard aggregate reflects the same split.
    adm = token(client, admin_user["username"], admin_user["password"])
    by_id = {c["class_id"]: c["pixels"] for c in client.get("/api/admin/class-distribution", headers=h(adm)).json()}
    assert by_id[1] == 20000 and by_id[2] == 30000 and by_id[3] == 15536


def test_class_counts_cached_on_submit(client, admin_user, operators, tiles):
    """A fully-painted submit writes JSON pixel counts to tiles.class_counts."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    tile_id = _classify(client, op_tok, class_id=2)

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["class_counts"] is not None
    cc = json.loads(row["class_counts"])
    assert cc == {"2": 65536}


def test_class_distribution_aggregates_pixels(client, admin_user, operators, tiles):
    """Endpoint sums pixel counts per class across all classified tiles."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    _classify(client, op_tok, class_id=1)
    _classify(client, op_tok, class_id=1)
    _classify(client, op_tok, class_id=3)

    adm = token(client, admin_user["username"], admin_user["password"])
    body = client.get("/api/admin/class-distribution", headers=h(adm)).json()
    by_id = {c["class_id"]: c for c in body}
    assert by_id[1]["pixels"] == 65536 * 2
    assert by_id[3]["pixels"] == 65536 * 1
    # Pct rounds to 2 dp; sum within tolerance.
    pct_sum = sum(c["pct"] for c in body)
    assert abs(pct_sum - 100.0) < 0.05


def test_class_distribution_includes_color_and_name(client, admin_user, operators, tiles):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    _classify(client, op_tok, class_id=1)
    adm = token(client, admin_user["username"], admin_user["password"])
    body = client.get("/api/admin/class-distribution", headers=h(adm)).json()
    c1 = next(c for c in body if c["class_id"] == 1)
    assert c1["name"] == "Massa d'água"  # default project's class id=1 (config.yaml seed)
    assert c1["color"].startswith("#") and len(c1["color"]) == 7


def test_class_distribution_scoped_to_project(client, admin_user, operators, tiles):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    _classify(client, op_tok, class_id=1)
    adm = token(client, admin_user["username"], admin_user["password"])
    scoped = client.get("/api/admin/class-distribution?project_id=1", headers=h(adm)).json()
    other = client.get("/api/admin/class-distribution?project_id=99", headers=h(adm)).json()
    assert any(c["pixels"] for c in scoped)
    assert other == []  # no tiles in nonexistent project


def test_recompute_class_counts_backfills_legacy(client, admin_user, operators, tiles):
    """Existing classified tiles with NULL class_counts get backfilled."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    tile_id = _classify(client, op_tok, class_id=4)
    from backend.database import connect, transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute("UPDATE tiles SET class_counts=NULL WHERE id=?", (tile_id,))

    from backend.scripts.recompute_class_counts import main as recompute
    import sys
    old_argv = sys.argv
    sys.argv = ["recompute_class_counts"]
    try:
        recompute()
    finally:
        sys.argv = old_argv

    conn = connect()
    try:
        cc = json.loads(conn.execute(
            "SELECT class_counts FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()["class_counts"])
    finally:
        conn.close()
    assert cc == {"4": 65536}
