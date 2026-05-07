"""Cross-project leakage guards.

A user is a member of project A only — they must never see, fetch, or be
assigned tiles from project B, even when no project_id is passed and even
when both projects share the same status mix."""
import sqlite3
import pytest
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _stub_mbtiles(tmp_path, name="stub.mbtiles") -> str:
    p = tmp_path / name
    if not p.exists():
        # A real (but minimal) mbtiles so the reader pool can open it during
        # /xyz tests. For path-existence checks even an empty file would do,
        # but layered tests benefit from a valid schema.
        c = sqlite3.connect(p)
        c.executescript(
            "CREATE TABLE metadata(name TEXT, value TEXT);"
            "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB,"
            " PRIMARY KEY(zoom_level, tile_column, tile_row));"
            "INSERT INTO metadata VALUES('format','png'),('minzoom','0'),('maxzoom','3');"
        )
        c.commit()
        c.close()
    return str(p)


@pytest.fixture()
def two_projects(client, admin_user, operators, tmp_path):
    """Set up:
       - Default project (seeded), with op1 as member.
       - Second project 'beta', with op2 as member.
    Returns (admin_token, default_pid=1, beta_pid)."""
    admin_tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "beta",
            "primary_mbtiles": _stub_mbtiles(tmp_path, "beta.mbtiles"),
            "classes": [
                {"id": 1, "name": "a", "color": "#001122"},
                {"id": 2, "name": "b", "color": "#334455"},
            ],
        },
        headers=h(admin_tok),
    ).json()
    beta_pid = new["id"]

    # Membership: op1 = default only (already added by fixture); op2 = beta only.
    # op2 was auto-added to default by the fixture; remove that membership so
    # we can prove leakage doesn't happen.
    client.delete(
        f"/api/admin/projects/1/members/{operators[1]['id']}", headers=h(admin_tok)
    )
    client.post(
        f"/api/admin/projects/{beta_pid}/members",
        json={"user_id": operators[1]["id"], "role": "operator"},
        headers=h(admin_tok),
    )
    return admin_tok, 1, beta_pid


def _seed_pending(conn, project_id: int, n: int):
    from backend.mask_utils import empty_mask_png
    empty = empty_mask_png()
    for i in range(n):
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
               status,data_png) VALUES (?,?,?,?,?,?,'pending',?)""",
            (project_id, f"p{project_id}_t{i}", 0.0 + i, 0.0, 0.1 + i, 0.1, empty),
        )


def test_next_serves_only_member_project_tiles(client, admin_user, operators, two_projects):
    admin_tok, default_pid, beta_pid = two_projects
    from backend.database import connect
    conn = connect()
    try:
        _seed_pending(conn, default_pid, 3)
        _seed_pending(conn, beta_pid, 3)
    finally:
        conn.close()

    op1 = token(client, operators[0]["username"], operators[0]["password"])
    op2 = token(client, operators[1]["username"], operators[1]["password"])

    # op1 is member of default only.
    r = client.get(f"/api/tiles/next?project_id={default_pid}", headers=h(op1))
    assert r.status_code == 200
    assert r.json()["project_id"] == default_pid
    # op1 cannot pull from beta.
    r = client.get(f"/api/tiles/next?project_id={beta_pid}", headers=h(op1))
    assert r.status_code == 403

    # op2 is member of beta only.
    r = client.get(f"/api/tiles/next?project_id={beta_pid}", headers=h(op2))
    assert r.status_code == 200
    assert r.json()["project_id"] == beta_pid
    r = client.get(f"/api/tiles/next?project_id={default_pid}", headers=h(op2))
    assert r.status_code == 403


def test_next_without_project_id_picks_unique_membership(client, admin_user, operators, two_projects):
    """When the user belongs to exactly one project, omitting project_id
    auto-resolves to it (zero-friction default for single-project deployments)."""
    _, default_pid, beta_pid = two_projects
    from backend.database import connect
    conn = connect()
    try:
        _seed_pending(conn, beta_pid, 1)
    finally:
        conn.close()
    op2 = token(client, operators[1]["username"], operators[1]["password"])
    r = client.get("/api/tiles/next", headers=h(op2))
    assert r.status_code == 200
    assert r.json()["project_id"] == beta_pid


def test_next_without_project_id_demands_one_when_multiple(client, admin_user, operators, two_projects):
    """When the user belongs to several projects, the API must refuse to
    guess and surface a clear 400 instead of silently picking one."""
    admin_tok, default_pid, beta_pid = two_projects
    # Re-add op1 to beta so they have two memberships.
    client.post(
        f"/api/admin/projects/{beta_pid}/members",
        json={"user_id": operators[0]["id"], "role": "operator"},
        headers=h(admin_tok),
    )
    op1 = token(client, operators[0]["username"], operators[0]["password"])
    r = client.get("/api/tiles/next", headers=h(op1))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "project_id_required"


def test_queue_stats_scoped_to_project(client, admin_user, operators, two_projects):
    _, default_pid, beta_pid = two_projects
    from backend.database import connect
    conn = connect()
    try:
        _seed_pending(conn, default_pid, 4)
        _seed_pending(conn, beta_pid, 7)
    finally:
        conn.close()
    op1 = token(client, operators[0]["username"], operators[0]["password"])
    a = client.get(f"/api/tiles/queue-stats?project_id={default_pid}", headers=h(op1)).json()
    assert a["total"] == 4

    op2 = token(client, operators[1]["username"], operators[1]["password"])
    b = client.get(f"/api/tiles/queue-stats?project_id={beta_pid}", headers=h(op2)).json()
    assert b["total"] == 7


def test_mask_complete_required_false_accepts_partial(client, admin_user, operators, tmp_path):
    """A project with mask_complete_required=False accepts a submit with
    255 pixels — the unfilled_pixels guard is bypassed for that project."""
    import numpy as np
    admin_tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "loose",
            "primary_mbtiles": _stub_mbtiles(tmp_path, "loose.mbtiles"),
            "mask_complete_required": False,
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(admin_tok),
    ).json()
    pid = new["id"]
    op = operators[0]
    client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(admin_tok),
    )
    from backend.database import connect
    from backend.mask_utils import empty_mask_png
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
               status,data_png) VALUES (?,?,?,?,?,?,'pending',?)""",
            (pid, "t1", 0.0, 0.0, 0.1, 0.1, empty_mask_png()),
        )
    finally:
        conn.close()
    op_tok = token(client, op["username"], op["password"])
    nxt = client.get(f"/api/tiles/next?project_id={pid}", headers=h(op_tok)).json()
    # Submit a half-painted mask: 50% class 1, 50% still 255.
    arr = np.full(65536, 255, dtype=np.uint8)
    arr[: 65536 // 2] = 1
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    assert r.status_code == 200, r.text


def test_mask_complete_required_true_rejects_partial(client, admin_user, operators, tiles):
    """Default project has mask_complete_required=True (seeded). A partial
    submit must be rejected with the existing unfilled_pixels error."""
    import numpy as np
    op = operators[0]
    op_tok = token(client, op["username"], op["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    arr = np.full(65536, 255, dtype=np.uint8)
    arr[: 65536 // 2] = 1
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=arr.tobytes(),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unfilled_pixels"


# ---- Admin dashboard scoping ------------------------------------------------

def test_admin_dashboard_scoped_to_project(client, admin_user, operators, two_projects):
    """Filtering admin/dashboard by project counts only that project's tiles."""
    _, default_pid, beta_pid = two_projects
    from backend.database import connect
    conn = connect()
    try:
        _seed_pending(conn, default_pid, 5)
        _seed_pending(conn, beta_pid, 9)
    finally:
        conn.close()
    adm = token(client, admin_user["username"], admin_user["password"])
    a = client.get(f"/api/admin/dashboard?project_id={default_pid}", headers=h(adm)).json()
    b = client.get(f"/api/admin/dashboard?project_id={beta_pid}", headers=h(adm)).json()
    g = client.get("/api/admin/dashboard", headers=h(adm)).json()
    assert a["total_tiles"] == 5
    assert b["total_tiles"] == 9
    assert g["total_tiles"] == 14  # global view sums both projects


def test_admin_tiles_listing_scoped_to_project(client, admin_user, operators, two_projects):
    """The admin tiles listing accepts project_id and the X-Total-Count header
    reflects the filtered count, not the global count."""
    _, default_pid, beta_pid = two_projects
    from backend.database import connect
    conn = connect()
    try:
        _seed_pending(conn, default_pid, 4)
        _seed_pending(conn, beta_pid, 6)
    finally:
        conn.close()
    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.get(f"/api/admin/tiles?project_id={beta_pid}", headers=h(adm))
    assert r.status_code == 200
    assert r.headers["x-total-count"] == "6"
    items = r.json()
    assert all(t["project_id"] == beta_pid for t in items)
