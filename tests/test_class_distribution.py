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
