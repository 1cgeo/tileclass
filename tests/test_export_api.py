"""Admin export endpoint: GET /api/admin/projects/{id}/export streams a ZIP of
the project's finished data, format following the project kind. Covers both
kinds (raster, classification), the status filter, the admin-only gate, and
the empty-project case."""
import io
import zipfile

from tests.conftest import token
from tests._helpers import auth as h, make_real_mbtiles


def _adm(client, admin_user):
    return token(client, admin_user["username"], admin_user["password"])


def _names(resp):
    return zipfile.ZipFile(io.BytesIO(resp.content)).namelist()


def _insert(pid, **cols):
    from backend.database import connect
    keys = ",".join(cols)
    qs = ",".join("?" * len(cols))
    conn = connect()
    try:
        conn.execute(
            f"INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,{keys}) "
            f"VALUES (?,?,?,?,?,?,{qs})",
            (pid, cols.get("name", "t"), -50.0, -25.0, -49.99, -24.99, *cols.values()),
        )
        conn.commit()
    finally:
        conn.close()


# ---- per-kind happy paths --------------------------------------------------

def test_export_raster_zip(client, admin_user, tiles):
    """Default project (id=1) is raster → GeoTIFFs + manifest."""
    from backend.mask_utils import encode_mask
    adm = _adm(client, admin_user)
    _insert(1, name="rt", status="reviewed",
            data_png=encode_mask(bytes([1]) * 65536, 256), reviewed_at="2026-01-01T00:00:00+00:00")
    r = client.get("/api/admin/projects/1/export?status=reviewed", headers=h(adm))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    names = _names(r)
    assert "manifest.csv" in names
    assert any(n.endswith(".tif") for n in names)
    assert r.headers["x-tile-count"] == "1"


def test_export_classification_zip(client, admin_user, tmp_path):
    adm = _adm(client, admin_user)
    proj = client.post("/api/admin/projects", json={
        "name": "cexp", "kind": "classification",
        "primary_mbtiles": make_real_mbtiles(tmp_path, "cexp.mbtiles"),
        "classes": [{"id": 1, "name": "a", "color": "#112233"}],
    }, headers=h(adm)).json()
    _insert(proj["id"], name="ct", status="reviewed", data_class_id=1,
            reviewed_at="2026-01-01T00:00:00+00:00")
    r = client.get(f"/api/admin/projects/{proj['id']}/export?status=reviewed", headers=h(adm))
    assert r.status_code == 200
    names = _names(r)
    assert "classifications.csv" in names
    # The CSV carries the class row.
    csv_text = zipfile.ZipFile(io.BytesIO(r.content)).read("classifications.csv").decode()
    assert "ct" in csv_text


# ---- status filter ---------------------------------------------------------

def test_status_filters(client, admin_user, tiles):
    """reviewed → only reviewed; classified → only classified;
    reviewed_classified → both. (Agents rely on all three via the CLI too.)"""
    from backend.mask_utils import encode_mask
    adm = _adm(client, admin_user)
    png = encode_mask(bytes([1]) * 65536, 256)
    _insert(1, name="rev", status="reviewed", data_png=png, reviewed_at="2026-01-01T00:00:00+00:00")
    _insert(1, name="cls", status="classified", data_png=png, classified_at="2026-01-01T00:00:00+00:00")
    counts = {}
    for s in ("reviewed", "classified", "reviewed_classified"):
        counts[s] = client.get(f"/api/admin/projects/1/export?status={s}", headers=h(adm)).headers["x-tile-count"]
    assert counts == {"reviewed": "1", "classified": "1", "reviewed_classified": "2"}


# ---- gates -----------------------------------------------------------------

def test_cli_run_honors_classified_filter(client, admin_user, tiles, tmp_path):
    """The script-level run() (the path agents call) honors the 'classified'
    filter and exports all kinds via the same STATUS_FILTERS keys."""
    from backend.mask_utils import encode_mask
    from backend.scripts import export_tiles
    png = encode_mask(bytes([1]) * 65536, 256)
    _insert(1, name="rev2", status="reviewed", data_png=png, reviewed_at="2026-01-01T00:00:00+00:00")
    _insert(1, name="cls2", status="classified", data_png=png, classified_at="2026-01-01T00:00:00+00:00")
    out = tmp_path / "cli"
    n = export_tiles.run(out, status="classified", project_id=1)
    assert n == 1
    assert [p.name for p in out.glob("gt_*.tif")] == ["gt_cls2.tif"]
    # And 'reviewed+classified' (CLI key, with the +) yields both.
    out2 = tmp_path / "cli2"
    assert export_tiles.run(out2, status="reviewed+classified", project_id=1) == 2


def test_export_requires_admin(client, operators, tiles):
    op = token(client, operators[0]["username"], operators[0]["password"])
    assert client.get("/api/admin/projects/1/export?status=reviewed", headers=h(op)).status_code == 403


def test_export_rejects_invalid_status(client, admin_user, tiles):
    adm = _adm(client, admin_user)
    # Pattern-validated query param → 422 from FastAPI before the handler.
    assert client.get("/api/admin/projects/1/export?status=bogus", headers=h(adm)).status_code == 422


def test_export_empty_project_returns_zip_with_manifest(client, admin_user, tiles):
    """A project with no matching tiles still yields a valid ZIP (manifest only,
    count 0) — the admin gets clear feedback rather than an error."""
    adm = _adm(client, admin_user)
    r = client.get("/api/admin/projects/1/export?status=reviewed", headers=h(adm))
    assert r.status_code == 200
    assert r.headers["x-tile-count"] == "0"
    assert "manifest.csv" in _names(r)


def test_export_404_for_missing_project(client, admin_user):
    adm = _adm(client, admin_user)
    assert client.get("/api/admin/projects/9999/export?status=reviewed", headers=h(adm)).status_code == 404
