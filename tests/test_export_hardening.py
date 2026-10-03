"""Raster export hardening: PROJ env in a fresh CLI process, per-project EDGV
remap policy (auto/edgv/raw), safe + unique per-tile filenames, and per-project
mosaics. Every test reopens the real GeoTIFF/manifest/zip it asserts on."""
import csv
import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import token
from tests._helpers import auth as h, make_real_mbtiles

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- helpers ---------------------------------------------------------------

def _seed_raster(pid, name, fill, tile_px=256, status="reviewed",
                 bbox=(-50.0, -25.0, -49.99, -24.99)):
    from backend.database import connect
    from backend.mask_utils import encode_mask
    if isinstance(fill, np.ndarray):
        raw = fill.astype(np.uint8).tobytes()
    else:
        raw = bytes([fill]) * (tile_px * tile_px)
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,data_png,reviewed_at,classified_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (pid, name, *bbox, status, encode_mask(raw, tile_px)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _create_project(client, adm, tmp_path, name, classes, tile_px=256):
    r = client.post("/api/admin/projects", json={
        "name": name, "kind": "raster", "tile_px": tile_px, "meters_per_pixel": 2.5,
        "primary_mbtiles": make_real_mbtiles(tmp_path, f"{name}.mbtiles"),
        "classes": classes,
    }, headers=h(adm))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _adm(client, admin_user):
    return token(client, admin_user["username"], admin_user["password"])


def _read(path):
    import rasterio
    with rasterio.open(path) as ds:
        return ds.read(1), ds.crs.to_epsg(), ds.width


def _manifest(out_dir):
    with (Path(out_dir) / "manifest.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


_CUSTOM = [{"id": 1, "name": "telhado", "color": "#e41a1c"},
           {"id": 2, "name": "piscina", "color": "#377eb8"}]


# ---- Bug 1: PROJ env in a fresh interpreter --------------------------------

def test_export_cli_fresh_process_with_bogus_proj_lib(app_env, default_project, tmp_path):
    """The CLI must find rasterio's proj.db even when the parent env carries a
    poisoned PROJ_LIB/PROJ_DATA (PostGIS on Windows) and runs inside a venv."""
    import yaml
    from backend.config import get_config
    _seed_raster(default_project, "fresh", 1)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(get_config()), encoding="utf-8")
    bogus = tmp_path / "no_proj_here"
    bogus.mkdir()
    env = {**os.environ, "TILECLASS_CONFIG": str(cfg_path),
           "PROJ_LIB": str(bogus), "PROJ_DATA": str(bogus)}
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, "-m", "backend.scripts.export_tiles", str(out), "--project", "1"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    arr, epsg, width = _read(out / "gt_fresh.tif")
    assert epsg == 4326 and width == 256
    assert (arr == 0).all()  # class 1 → EDGV 0 (default palette is the 6c one)


@pytest.mark.parametrize("module", ["backend.scripts.build_mbtiles", "backend.mask_tile_service"])
def test_proj_env_fixed_for_other_rasterio_modules(module, tmp_path):
    bogus = tmp_path / "nope"
    bogus.mkdir()
    env = {**os.environ, "PROJ_LIB": str(bogus), "PROJ_DATA": str(bogus)}
    code = f"import {module}; from rasterio.crs import CRS; print(CRS.from_epsg(4326).to_epsg())"
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0 and r.stdout.strip().endswith("4326"), r.stderr


# ---- Bug 2: remap policy ----------------------------------------------------

def test_auto_remap_keeps_custom_palette_ids(client, admin_user, tmp_path):
    from backend.scripts import export_tiles
    pid = _create_project(client, _adm(client, admin_user), tmp_path, "custom", _CUSTOM)
    _seed_raster(pid, "c1", 1)
    export_tiles.run(tmp_path / "o", status="reviewed", project_id=pid)
    arr, _, _ = _read(tmp_path / "o" / "gt_c1.tif")
    assert (arr == 1).all()  # NOT permuted to EDGV 0
    assert _manifest(tmp_path / "o")[0]["remap"] == "raw"


def test_auto_remap_applies_edgv_on_legacy_palette(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    _seed_raster(1, "legacy", 3)
    export_tiles.run(tmp_path, status="reviewed", project_id=1)
    arr, _, _ = _read(tmp_path / "gt_legacy.tif")
    assert (arr == 4).all()  # floresta 3 → EDGV 4
    assert _manifest(tmp_path)[0]["remap"] == "edgv"


def test_forced_edgv_and_raw(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    pid = _create_project(client, _adm(client, admin_user), tmp_path, "custom2", _CUSTOM)
    _seed_raster(pid, "c2", 1)
    _seed_raster(1, "d2", 6)
    export_tiles.run(tmp_path / "e", status="reviewed", project_id=pid, remap="edgv")
    assert (_read(tmp_path / "e" / "gt_c2.tif")[0] == 0).all()
    assert _manifest(tmp_path / "e")[0]["remap"] == "edgv"
    export_tiles.run(tmp_path / "r", status="reviewed", project_id=1, remap="raw")
    assert (_read(tmp_path / "r" / "gt_d2.tif")[0] == 6).all()
    # Legacy kwarg `raw=True` still forces raw.
    export_tiles.run(tmp_path / "r2", status="reviewed", project_id=1, raw=True)
    assert (_read(tmp_path / "r2" / "gt_d2.tif")[0] == 6).all()
    with pytest.raises(ValueError):
        export_tiles.run(tmp_path / "x", status="reviewed", project_id=1, remap="bogus")


def test_cli_edgv_flag_and_mutual_exclusion(client, admin_user, tmp_path, monkeypatch):
    from backend.scripts import export_tiles
    pid = _create_project(client, _adm(client, admin_user), tmp_path, "custom3", _CUSTOM)
    _seed_raster(pid, "c3", 2)
    out = tmp_path / "cli"
    monkeypatch.setattr(sys, "argv", ["export_tiles", str(out), "--project", str(pid), "--edgv"])
    export_tiles.main()
    assert (_read(out / "gt_c3.tif")[0] == 1).all()  # 2 → EDGV 1
    monkeypatch.setattr(sys, "argv", ["export_tiles", str(out), "--raw", "--edgv"])
    with pytest.raises(SystemExit):
        export_tiles.main()


def test_api_export_remap_param(client, admin_user, tiles):
    adm = _adm(client, admin_user)
    _seed_raster(1, "api", 6)

    def tif(resp):
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        import rasterio
        from rasterio.io import MemoryFile
        with MemoryFile(zf.read("gt_api.tif")) as mf, mf.open() as ds:
            return ds.read(1), zf.read("manifest.csv").decode()

    auto = client.get("/api/admin/projects/1/export?status=reviewed", headers=h(adm))
    arr, man = tif(auto)
    assert (arr == 2).all() and ",edgv" in man
    raw = client.get("/api/admin/projects/1/export?status=reviewed&remap=raw", headers=h(adm))
    arr, man = tif(raw)
    assert (arr == 6).all() and ",raw" in man
    assert client.get("/api/admin/projects/1/export?status=reviewed&remap=nope",
                      headers=h(adm)).status_code == 422


def test_export_job_remap_param(client, admin_user, tmp_path):
    from backend import export_service
    from rasterio.io import MemoryFile
    pid = _create_project(client, _adm(client, admin_user), tmp_path, "custom4", _CUSTOM)
    _seed_raster(pid, "j", 1)
    job = export_service.create_job(pid, "reviewed", by_user=admin_user["id"],
                                    remap="edgv", run_async=False)
    assert job["state"] == "done", job
    with zipfile.ZipFile(job["file_path"]) as zf, MemoryFile(zf.read("gt_j.tif")) as mf, mf.open() as ds:
        assert (ds.read(1) == 0).all()
    adm = _adm(client, admin_user)
    assert client.post(f"/api/admin/projects/{pid}/export-jobs?status=reviewed&remap=bad",
                       headers=h(adm)).status_code == 422
    ok = client.post(f"/api/admin/projects/{pid}/export-jobs?status=reviewed&remap=raw",
                     headers=h(adm))
    assert ok.status_code == 200, ok.text
    # Wait for the worker thread so it never outlives this test's DB.
    import time
    for _ in range(100):
        st = client.get(f"/api/admin/export-jobs/{ok.json()['id']}", headers=h(adm)).json()["state"]
        if st in ("done", "error"):
            break
        time.sleep(0.1)
    assert st == "done"
    with zipfile.ZipFile(export_service.job_artifact(ok.json()["id"])[0]) as zf, \
            MemoryFile(zf.read("gt_j.tif")) as mf, mf.open() as ds:
        assert (ds.read(1) == 1).all()  # raw kept


# ---- Bug 3: unsafe / duplicate tile names ----------------------------------

def test_duplicate_names_do_not_overwrite(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    a = _seed_raster(1, "dup", 1, bbox=(0.0, 0.0, 0.1, 0.1))
    b = _seed_raster(1, "dup", 2, bbox=(0.1, 0.0, 0.2, 0.1))
    _seed_raster(1, "DUP", 3, bbox=(0.2, 0.0, 0.3, 0.1))  # case-insensitive FS
    out = tmp_path / "o"
    assert export_tiles.run(out, status="reviewed", project_id=1, mosaic=True) == 3
    rows = _manifest(out)
    fnames = [r["filename"] for r in rows]
    assert len({f.lower() for f in fnames}) == 3
    assert len(list(out.glob("gt_*.tif"))) == 4  # 3 tiles + mosaic
    expected = {str(a): 0, str(b): 1}  # EDGV of classes 1,2
    for r in rows:
        arr, _, _ = _read(out / r["filename"])
        if r["tile_id"] in expected:
            assert (arr == expected[r["tile_id"]]).all()
    mosaic, _, _ = _read(out / "gt_mosaic.tif")
    assert {0, 1, 4}.issubset(set(np.unique(mosaic).tolist()))


@pytest.mark.parametrize("name", ["a/b", "x:y", "../escape", "..\\esc2", "...hidden", "ç ã"])
def test_unsafe_names_are_sanitized(client, admin_user, tiles, tmp_path, name):
    from backend.scripts import export_tiles
    _seed_raster(1, name, 1)
    out = tmp_path / "sub" / "o"
    assert export_tiles.run(out, status="reviewed", project_id=1) == 1
    fname = _manifest(out)[0]["filename"]
    assert fname.startswith("gt_") and fname.endswith(".tif")
    stem = fname[3:-4]
    assert stem and not stem.startswith(".")
    assert all(c.isascii() and (c.isalnum() or c in "._-") for c in stem)
    assert (out / fname).is_file() and (out / fname).stat().st_size > 0
    assert _read(out / fname)[1] == 4326
    # Nothing escaped out_dir, no stray stream-host file next to it.
    assert sorted(p.name for p in out.iterdir()) == sorted([fname, "manifest.csv"])
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["o"]


def test_tile_named_mosaic_not_clobbered(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    _seed_raster(1, "mosaic", 1)
    export_tiles.run(tmp_path, status="reviewed", project_id=1, mosaic=True)
    fname = _manifest(tmp_path)[0]["filename"]
    assert fname != "gt_mosaic.tif"
    assert (tmp_path / fname).is_file() and (tmp_path / "gt_mosaic.tif").is_file()


# ---- Bug 8: per-project mosaic ---------------------------------------------

def test_multi_project_mosaic_is_per_project(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    pid = _create_project(client, _adm(client, admin_user), tmp_path, "px64",
                          _CUSTOM, tile_px=64)
    _seed_raster(1, "big", 1, bbox=(0.0, 0.0, 0.1, 0.1))
    _seed_raster(pid, "small", 2, tile_px=64, bbox=(0.05, 0.0, 0.15, 0.1))
    out = tmp_path / "all"
    assert export_tiles.run(out, status="reviewed", project_id=None, mosaic=True) == 2
    assert not (out / "gt_mosaic.tif").exists()
    m1, _, w1 = _read(out / "gt_mosaic_p1.tif")
    m2, _, w2 = _read(out / f"gt_mosaic_p{pid}.tif")
    assert w1 == 256 and w2 == 64
    assert set(np.unique(m1).tolist()) == {0}       # project 1: EDGV 0 only
    assert set(np.unique(m2).tolist()) == {2}       # custom palette: raw id 2 only
