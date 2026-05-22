"""Tile ingestion via the admin UI endpoint (POST /api/admin/projects/{id}/tiles)
and the shared tile_ingest service. Mirrors the import_points CLI behaviour:
geodesic bbox from the project geometry, NxN adjacent blocks, per-(project,bbox)
dedup, raster body seed vs NULL for other kinds."""
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _stub_mbtiles(tmp_path, name="stub.mbtiles"):
    p = tmp_path / name
    p.write_bytes(b"")
    return str(p)


def _create_project(client, adm, tmp_path, kind="raster", name="ing"):
    body = {"name": name, "kind": kind, "primary_mbtiles": _stub_mbtiles(tmp_path)}
    if kind == "vector":
        body["attributes"] = [{"key": "tipo", "type": "text", "label": "Tipo"}]
    else:
        body["classes"] = [{"id": 1, "name": "a", "color": "#112233"}]
    r = client.post("/api/admin/projects", json=body, headers=h(adm))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _count(project_id):
    from backend.database import connect
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM tiles WHERE project_id=?", (project_id,)).fetchone()["c"]
    finally:
        conn.close()


def test_add_single_point_creates_one_pending_raster_tile(client, admin_user, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    r = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm),
                    json={"points": [{"lat": -23.55, "lon": -46.63, "name": "sp"}]})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1 and r.json()["skipped"] == 0
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status, data_png, name FROM tiles WHERE project_id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending" and row["name"] == "sp"
    assert row["data_png"] is not None  # raster gets an empty-mask seed


def test_block_creates_n_squared_adjacent_tiles(client, admin_user, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    r = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm),
                    json={"points": [{"lat": 0.0, "lon": 0.0, "name": "c"}], "block": 3})
    assert r.json()["inserted"] == 9
    assert _count(pid) == 9


def test_dedup_skips_same_bbox_on_reimport(client, admin_user, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    body = {"points": [{"lat": -10.0, "lon": -40.0, "name": "x"}]}
    assert client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm), json=body).json()["inserted"] == 1
    again = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm), json=body).json()
    assert again["inserted"] == 0 and again["skipped"] == 1
    assert _count(pid) == 1


def test_vector_project_seeds_null_body(client, admin_user, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path, kind="vector", name="ingvec")
    client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm),
                json={"points": [{"lat": 1.0, "lon": 1.0, "name": "v"}]})
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT data_png, data_geojson FROM tiles WHERE project_id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["data_png"] is None and row["data_geojson"] is None


def test_csv_style_bulk_points(client, admin_user, tmp_path):
    """The UI parses a CSV client-side into a points array — the endpoint just
    takes the array. Three distinct points → three tiles."""
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    pts = [{"lat": -1.0, "lon": -1.0, "name": "a"},
           {"lat": -2.0, "lon": -2.0, "name": "b"},
           {"lat": -3.0, "lon": -3.0}]  # name omitted → derived
    r = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm), json={"points": pts})
    assert r.json()["inserted"] == 3 and _count(pid) == 3


def test_rejects_even_block_and_bad_coords(client, admin_user, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    even = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm),
                       json={"points": [{"lat": 0, "lon": 0}], "block": 2})
    assert even.status_code == 400 and even.json()["detail"]["error"] == "invalid_block"
    oob = client.post(f"/api/admin/projects/{pid}/tiles", headers=h(adm),
                      json={"points": [{"lat": 200, "lon": 0}]})
    assert oob.status_code == 400 and oob.json()["detail"]["error"] == "coordinate_out_of_range"


def test_add_tiles_requires_admin_and_existing_project(client, admin_user, operators, tmp_path):
    adm = token(client, admin_user["username"], admin_user["password"])
    pid = _create_project(client, adm, tmp_path)
    op = token(client, operators[0]["username"], operators[0]["password"])
    assert client.post(f"/api/admin/projects/{pid}/tiles", headers=h(op),
                       json={"points": [{"lat": 0, "lon": 0}]}).status_code == 403
    missing = client.post("/api/admin/projects/99999/tiles", headers=h(adm),
                          json={"points": [{"lat": 0, "lon": 0}]})
    assert missing.status_code == 404 and missing.json()["detail"]["error"] == "project_not_found"
