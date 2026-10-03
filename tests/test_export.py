"""Dedicated export tests: GeoTIFF content (EDGV remap, NODATA, CRS), --raw,
--mosaic, non-256 geometry, per-kind exporter isolation, manifest content, and
the export_service ZIP packaging shared by the admin endpoint.

Complements test_export_api.py (HTTP layer) and the per-kind suites by locking
the actual bytes/values agents consume."""
import csv
import io
import zipfile

import numpy as np

from tests.conftest import token
from tests._helpers import auth as h, make_real_mbtiles


# ---- helpers ---------------------------------------------------------------

def _adm(client, admin_user):
    return token(client, admin_user["username"], admin_user["password"])


def _seed_raster(pid, name, mask_bytes, tile_px=256, status="reviewed",
                 bbox=(-50.0, -25.0, -49.99, -24.99)):
    from backend.database import connect
    from backend.mask_utils import encode_mask
    png = encode_mask(mask_bytes, tile_px)
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,data_png,reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
            (pid, name, *bbox, status, png),
        )
        conn.commit()
    finally:
        conn.close()


def _create(client, adm, body):
    r = client.post("/api/admin/projects", json=body, headers=h(adm))
    assert r.status_code == 200, r.text
    return r.json()


# ---- raster GeoTIFF content ------------------------------------------------

def test_raster_export_applies_edgv_remap(client, admin_user, tiles, tmp_path):
    """Default (remap=auto) remaps TileClass IDs → EDGV (1→0, 3→4, 6→2) because
    the default project uses the legacy {1..6} palette (custom palettes stay
    raw — see test_export_hardening.py). Reopen the GeoTIFF and assert the
    pixel values, NODATA, CRS, dtype and band count."""
    import rasterio
    from backend.scripts import export_tiles
    # Mask: top half class 1 (água), bottom half class 3 (floresta).
    mask = np.full((256, 256), 1, dtype=np.uint8)
    mask[128:, :] = 3
    _seed_raster(1, "remap", mask.tobytes())
    n = export_tiles.run(tmp_path, status="reviewed", project_id=1)
    assert n == 1
    with rasterio.open(tmp_path / "gt_remap.tif") as ds:
        assert ds.count == 1 and ds.dtypes[0] == "uint8"
        assert ds.nodata == 255
        assert ds.crs.to_epsg() == 4326
        arr = ds.read(1)
    assert (arr[:128, :] == 0).all()   # class 1 → EDGV 0
    assert (arr[128:, :] == 4).all()   # class 3 → EDGV 4


def test_raster_export_raw_keeps_native_ids(client, admin_user, tiles, tmp_path):
    import rasterio
    from backend.scripts import export_tiles
    mask = np.full((256, 256), 6, dtype=np.uint8)  # terreno exposto
    _seed_raster(1, "rawids", mask.tobytes())
    export_tiles.run(tmp_path, status="reviewed", project_id=1, raw=True)
    with rasterio.open(tmp_path / "gt_rawids.tif") as ds:
        arr = ds.read(1)
    assert (arr == 6).all()  # NOT remapped to EDGV 2


def test_raster_export_mosaic(client, admin_user, tiles, tmp_path):
    """The mosaic must actually merge the tiles' content (EDGV-remapped), not
    just be an empty file at the right path."""
    import rasterio
    from backend.scripts import export_tiles
    # Two adjacent tiles, classes 1 (água) and 2 (edif) → EDGV 0 and 1.
    _seed_raster(1, "m1", np.full(65536, 1, dtype=np.uint8).tobytes(), bbox=(0.0, 0.0, 0.1, 0.1))
    _seed_raster(1, "m2", np.full(65536, 2, dtype=np.uint8).tobytes(), bbox=(0.1, 0.0, 0.2, 0.1))
    export_tiles.run(tmp_path, status="reviewed", project_id=1, mosaic=True)
    mosaic = tmp_path / "gt_mosaic.tif"
    assert mosaic.exists()
    with rasterio.open(mosaic) as ds:
        arr = ds.read(1)
        assert ds.nodata == 255 and ds.crs.to_epsg() == 4326
    vals = set(np.unique(arr).tolist())
    assert 0 in vals and 1 in vals  # both tiles' remapped classes present in the mosaic


def test_raster_export_honours_non_256_tile_px(client, admin_user, tmp_path):
    """tile_px is per-project; the GeoTIFF must match (not hardcode 256)."""
    import rasterio
    from backend.scripts import export_tiles
    adm = _adm(client, admin_user)
    proj = _create(client, adm, {
        "name": "px64", "kind": "raster", "tile_px": 64, "meters_per_pixel": 2.5,
        "primary_mbtiles": make_real_mbtiles(tmp_path, "px64.mbtiles"),
        "classes": [{"id": 1, "name": "a", "color": "#112233"}],
    })
    _seed_raster(proj["id"], "small", np.full(64 * 64, 1, dtype=np.uint8).tobytes(), tile_px=64)
    export_tiles.run(tmp_path / "out", status="reviewed", project_id=proj["id"])
    with rasterio.open(tmp_path / "out" / "gt_small.tif") as ds:
        assert ds.width == 64 and ds.height == 64


# ---- per-kind exporter isolation ------------------------------------------

def test_exporters_isolate_by_kind(client, admin_user, tiles, tmp_path):
    """Each exporter must pick ONLY its own kind on a mixed-kind DB — running
    export_tiles must not choke on a classification tile (NULL data_png)."""
    from backend.scripts import export_tiles, export_classifications
    from backend.database import connect
    adm = _adm(client, admin_user)
    # Default project (id=1) is raster — give it a reviewed tile.
    _seed_raster(1, "r_tile", np.full(65536, 1, dtype=np.uint8).tobytes())
    cls = _create(client, adm, {"name": "ck", "kind": "classification",
                                "primary_mbtiles": make_real_mbtiles(tmp_path, "ck.mbtiles"),
                                "classes": [{"id": 1, "name": "c", "color": "#e41a1c"}]})
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,data_class_id,reviewed_at)
               VALUES (?,?,?,?,?,?,'reviewed',1,datetime('now'))""",
            (cls["id"], "c_tile", -50.0, -25.0, -49.99, -24.99),
        )
        conn.commit()
    finally:
        conn.close()

    # export_tiles over ALL projects: only the raster tile, no crash on the
    # classification one. Multi-project export prefixes the filename with
    # p<project_id>.
    n_raster = export_tiles.run(tmp_path / "rt", status="reviewed", project_id=None)
    assert n_raster == 1
    tifs = [p.name for p in (tmp_path / "rt").glob("gt_*.tif")]
    assert len(tifs) == 1 and "r_tile" in tifs[0] and tifs[0].startswith("gt_p")
    # The classification exporter sees only its own tile.
    assert export_classifications.run(tmp_path / "ct", status="reviewed", project_id=None) == 1
    csv_text = (tmp_path / "ct" / "classifications.csv").read_text(encoding="utf-8")
    assert "c_tile" in csv_text and "r_tile" not in csv_text


# ---- manifest content ------------------------------------------------------

def test_raster_manifest_columns(client, admin_user, tiles, tmp_path):
    from backend.scripts import export_tiles
    _seed_raster(1, "manif", np.full(65536, 2, dtype=np.uint8).tobytes())
    export_tiles.run(tmp_path, status="reviewed", project_id=1)
    with (tmp_path / "manifest.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if r["name"] == "manif")
    assert row["filename"] == "gt_manif.tif"
    assert row["status"] == "reviewed"
    assert row["project_id"] == "1"
    assert float(row["bbox_west"]) == -50.0 and float(row["bbox_north"]) == -24.99


# ---- export_service (ZIP packaging) ---------------------------------------

def test_export_service_zip_and_filename(client, admin_user, tiles):
    from backend import export_service
    _seed_raster(1, "svc", np.full(65536, 1, dtype=np.uint8).tobytes())
    data, fname, count = export_service.export_zip(1, "reviewed")
    assert count == 1
    # Filename: <sanitized project>_<status>_<YYYYMMDD>.zip
    assert fname.startswith("default_reviewed_") and fname.endswith(".zip")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "manifest.csv" in names and "gt_svc.tif" in names


def test_export_service_sanitizes_project_name(client, admin_user, tmp_path):
    """A project name with spaces/odd chars yields a safe ZIP filename."""
    from backend import export_service
    adm = _adm(client, admin_user)
    proj = _create(client, adm, {
        "name": "Proj X/2!", "kind": "classification",
        "primary_mbtiles": make_real_mbtiles(tmp_path, "px.mbtiles"),
        "classes": [{"id": 1, "name": "a", "color": "#112233"}],
    })
    _, fname, _ = export_service.export_zip(proj["id"], "reviewed")
    assert "/" not in fname and " " not in fname and "!" not in fname
    assert fname.endswith(".zip")


def test_export_service_invalid_status_and_missing_project(client, admin_user, tiles):
    import pytest
    from backend import export_service
    with pytest.raises(ValueError):
        export_service.export_zip(1, "bogus")
    with pytest.raises(LookupError):
        export_service.export_zip(99999, "reviewed")
