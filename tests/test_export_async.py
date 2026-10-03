"""Asynchronous export jobs: build the ZIP off the request thread, poll, and
download. Focused on the large-dataset path (no long synchronous request)."""
import io
import time
import zipfile

import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def _seed_reviewed_raster(project_id, name, fill=1):
    from backend.database import connect
    from backend.mask_utils import encode_mask
    png = encode_mask(np.full(65536, fill, dtype=np.uint8).tobytes())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
            "status,data_png,reviewed_at) VALUES (?,?,?,?,?,?, 'reviewed',?,datetime('now'))",
            (project_id, name, -50.0, -25.0, -49.99, -24.99, png),
        )
        conn.commit()
    finally:
        conn.close()


def test_create_job_sync_builds_downloadable_zip(client, admin_user, tiles):
    """Service-level deterministic path (run_async=False): job reaches done with
    a real zip artifact containing the exported tile."""
    from backend import export_service
    _seed_reviewed_raster(1, "rev1")
    job = export_service.create_job(1, "reviewed", by_user=admin_user["id"], run_async=False)
    assert job["state"] == "done" and job["tile_count"] == 1
    path, fname = export_service.job_artifact(job["id"])
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert any(n.endswith(".tif") for n in names) and "manifest.csv" in names
    assert fname.endswith(".zip")


def test_async_job_endpoints_poll_and_download(client, admin_user, tiles):
    """HTTP path: POST starts the job, GET polls until done, download streams
    the zip."""
    adm = token(client, admin_user["username"], admin_user["password"])
    _seed_reviewed_raster(1, "rev2")
    created = client.post("/api/admin/projects/1/export-jobs?status=reviewed", headers=h(adm))
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]
    assert created.json()["state"] in ("pending", "running", "done")

    state = None
    for _ in range(100):  # up to ~10s for the background thread
        r = client.get(f"/api/admin/export-jobs/{job_id}", headers=h(adm))
        assert r.status_code == 200
        state = r.json()["state"]
        if state in ("done", "error"):
            break
        time.sleep(0.1)
    assert state == "done", f"job did not finish: {state}"

    dl = client.get(f"/api/admin/export-jobs/{job_id}/download", headers=h(adm))
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert any(n.endswith(".tif") for n in zf.namelist())


def test_download_before_ready_returns_409(client, admin_user, tiles):
    from backend.database import connect, now_iso
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO export_jobs(project_id,status_param,state,created_by,created_at) "
            "VALUES (1,'reviewed','pending',?,?)",
            (admin_user["id"], now_iso()),
        )
        conn.commit()
        jid = conn.execute("SELECT MAX(id) m FROM export_jobs").fetchone()["m"]
    finally:
        conn.close()
    adm = token(client, admin_user["username"], admin_user["password"])
    r = client.get(f"/api/admin/export-jobs/{jid}/download", headers=h(adm))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_ready"


def test_create_job_rejects_bad_status_and_missing_project(client, admin_user):
    adm = token(client, admin_user["username"], admin_user["password"])
    # Pattern-validated query → 422.
    assert client.post("/api/admin/projects/1/export-jobs?status=bogus",
                       headers=h(adm)).status_code == 422
    miss = client.post("/api/admin/projects/99999/export-jobs?status=reviewed", headers=h(adm))
    assert miss.status_code == 404 and miss.json()["detail"]["error"] == "project_not_found"


def test_export_job_endpoints_require_admin(client, admin_user, operators, tiles):
    op = token(client, operators[0]["username"], operators[0]["password"])
    assert client.post("/api/admin/projects/1/export-jobs?status=reviewed",
                       headers=h(op)).status_code == 403
    assert client.get("/api/admin/export-jobs/1", headers=h(op)).status_code == 403
    assert client.get("/api/admin/export-jobs/1/download", headers=h(op)).status_code == 403


def test_job_not_found_returns_404(client, admin_user):
    adm = token(client, admin_user["username"], admin_user["password"])
    assert client.get("/api/admin/export-jobs/99999", headers=h(adm)).status_code == 404


# ---- Archive hygiene / restart recovery ------------------------------------

def test_exports_dir_is_isolated_per_test(app_env):
    """Tests must never write into the dev server's real %TEMP% export dir."""
    from backend import export_service
    assert app_env.parent in export_service.EXPORTS_DIR.parents


def test_create_job_prunes_archives_older_than_24h(client, admin_user, tiles):
    import os
    from backend import export_service
    d = export_service.EXPORTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    old, fresh, other = d / "job_9001.zip", d / "job_9002.zip", d / "notes.txt"
    for p in (old, fresh, other):
        p.write_bytes(b"x")
    stale = time.time() - 25 * 3600
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    _seed_reviewed_raster(1, "prune")
    job = export_service.create_job(1, "reviewed", by_user=admin_user["id"], run_async=False)
    assert job["state"] == "done"
    assert not old.exists()
    assert fresh.exists() and other.exists()
    assert (d / f"job_{job['id']}.zip").exists()


def test_startup_marks_interrupted_jobs_failed(app_env, admin_user):
    from backend.database import connect, now_iso
    from backend.main import app
    from fastapi.testclient import TestClient
    from tests.conftest import _seed_test_project
    conn = connect()
    try:
        _seed_test_project(conn)
        ids = {}
        for state in ("pending", "running", "done", "error"):
            cur = conn.execute(
                "INSERT INTO export_jobs(project_id,status_param,state,created_by,created_at) "
                "VALUES (1,'reviewed',?,?,?)", (state, admin_user["id"], now_iso()))
            ids[state] = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    with TestClient(app):
        pass
    conn = connect()
    try:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM export_jobs")}
    finally:
        conn.close()
    for st in ("pending", "running"):
        r = rows[ids[st]]
        assert r["state"] == "error" and "reinicializa" in r["error"] and r["finished_at"]
    assert rows[ids["done"]]["state"] == "done"
    assert rows[ids["error"]]["error"] is None


def test_failed_build_removes_partial_zip(client, admin_user, tiles, monkeypatch):
    from backend import export_service

    def broken(project_id, status_param, dest, *a, **k):
        dest.write_bytes(b"PK\x03\x04truncated")
        raise RuntimeError("disk full")

    monkeypatch.setattr(export_service, "_build_zip", broken)
    job = export_service.create_job(1, "reviewed", by_user=admin_user["id"], run_async=False)
    assert job["state"] == "error" and "disk full" in job["error"]
    assert not (export_service.EXPORTS_DIR / f"job_{job['id']}.zip").exists()
