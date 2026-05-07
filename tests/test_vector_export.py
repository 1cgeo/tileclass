"""export_features CLI: emits one FeatureCollection per tile + manifest.

Confirms the file/manifest contract used by ML pipelines downstream."""
import csv
import json
import sqlite3
import sys
from pathlib import Path


def _real_mbtiles(tmp_path, name="real.mbtiles"):
    path = tmp_path / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT,"
        " tile_data BLOB, PRIMARY KEY(zoom_level, tile_column, tile_row));"
    )
    conn.execute("INSERT INTO metadata VALUES('format','png'),('minzoom','0'),('maxzoom','3')")
    conn.commit(); conn.close()
    return str(path)


def _seed(client, admin_user, tmp_path, *, status="reviewed", count=2,
          name="exp", with_attrs=True):
    """Create a vector project + insert `count` tiles in `status` with
    real GeoJSON bodies. Returns (project_id, [tile_id, ...])."""
    from tests.conftest import token
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": name,
        "kind": "vector",
        "primary_mbtiles": _real_mbtiles(tmp_path, f"{name}.mbtiles"),
        "attributes": [
            {"key": "tipo", "type": "enum", "label": "Tipo",
             "required": True, "options": ["rio", "arroio"]},
        ] if with_attrs else [],
    }
    proj = client.post(
        "/api/admin/projects", json=body,
        headers={"Authorization": f"Bearer {tok}"},
    ).json()
    pid = proj["id"]
    from backend.database import connect
    conn = connect()
    ids = []
    try:
        for i in range(count):
            fc = json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "LineString",
                                  "coordinates": [[i, 0], [i + 0.1, 0.1]]},
                    "properties": {"tipo": "rio" if i % 2 == 0 else "arroio"},
                }],
            })
            conn.execute(
                """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                     bbox_east, bbox_north, status, data_geojson,
                                     feature_count, classified_at, reviewed_at)
                   VALUES (?,?,?,?,?,?, ?, ?, 1,
                           datetime('now'), datetime('now'))""",
                (pid, f"{name}_{i:02d}", float(i), 0.0, float(i) + 0.1, 0.1,
                 status, fc),
            )
            ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    finally:
        conn.close()
    return pid, ids


def _run_export(args: list[str]) -> int:
    """Drive export_features.main with custom argv; returns exit code."""
    import importlib
    if "backend.scripts.export_features" in sys.modules:
        del sys.modules["backend.scripts.export_features"]
    old_argv = sys.argv
    sys.argv = ["export_features", *args]
    try:
        mod = importlib.import_module("backend.scripts.export_features")
        try:
            mod.main()
        except SystemExit as e:
            return int(e.code or 0)
    finally:
        sys.argv = old_argv
    return 0


# ---- Per-tile FeatureCollection + manifest --------------------------------

def test_export_writes_per_tile_geojson(client, admin_user, tmp_path):
    pid, ids = _seed(client, admin_user, tmp_path, count=3)
    out_dir = tmp_path / "out"
    code = _run_export([str(out_dir), f"--project={pid}"])
    assert code == 0
    files = sorted(out_dir.glob("gt_*.geojson"))
    assert len(files) == 3
    # Each file is a valid FeatureCollection with one LineString.
    for p in files:
        doc = json.loads(p.read_text())
        assert doc["type"] == "FeatureCollection"
        assert len(doc["features"]) == 1
        assert doc["features"][0]["geometry"]["type"] == "LineString"


def test_export_manifest_has_feature_count(client, admin_user, tmp_path):
    pid, _ = _seed(client, admin_user, tmp_path, count=2)
    out_dir = tmp_path / "out"
    _run_export([str(out_dir), f"--project={pid}"])
    rows = list(csv.DictReader((out_dir / "manifest.csv").open()))
    assert len(rows) == 2
    assert all(int(r["feature_count"]) == 1 for r in rows)
    assert all(int(r["project_id"]) == pid for r in rows)


# ---- Status filter --------------------------------------------------------

def test_export_filters_by_status(client, admin_user, tmp_path):
    """Default status=reviewed. Tiles in `classified` are excluded unless
    --status reviewed+classified is passed."""
    pid_r, _ = _seed(client, admin_user, tmp_path, count=2,
                     name="r-only", status="reviewed")
    pid_c, _ = _seed(client, admin_user, tmp_path, count=3,
                     name="c-only", status="classified")
    out_default = tmp_path / "out_default"
    _run_export([str(out_default)])
    n_default = len(list(out_default.glob("gt_*.geojson")))

    out_both = tmp_path / "out_both"
    _run_export([str(out_both), "--status=reviewed+classified"])
    n_both = len(list(out_both.glob("gt_*.geojson")))
    assert n_both == n_default + 3  # 3 classified tiles added


# ---- Mosaic ---------------------------------------------------------------

def test_export_mosaic_unifies_with_tile_id(client, admin_user, tmp_path):
    pid, _ = _seed(client, admin_user, tmp_path, count=3, name="mos")
    out_dir = tmp_path / "mos_out"
    _run_export([str(out_dir), f"--project={pid}", "--mosaic"])
    mosaic = json.loads((out_dir / "gt_mosaic.geojson").read_text())
    assert mosaic["type"] == "FeatureCollection"
    assert len(mosaic["features"]) == 3
    # Each feature carries the source tile id + name for downstream joins.
    for f in mosaic["features"]:
        props = f["properties"]
        assert "_tile_id" in props
        assert "_tile_name" in props
        assert props["tipo"] in ("rio", "arroio")


# ---- Project filter -------------------------------------------------------

def test_export_filters_by_project(client, admin_user, tmp_path):
    pid_a, _ = _seed(client, admin_user, tmp_path, count=2, name="proj-a")
    pid_b, _ = _seed(client, admin_user, tmp_path, count=4, name="proj-b")
    out_a = tmp_path / "only-a"
    _run_export([str(out_a), f"--project={pid_a}"])
    assert len(list(out_a.glob("gt_*.geojson"))) == 2
    out_b = tmp_path / "only-b"
    _run_export([str(out_b), f"--project={pid_b}"])
    assert len(list(out_b.glob("gt_*.geojson"))) == 4
    out_all = tmp_path / "all"
    _run_export([str(out_all)])
    assert len(list(out_all.glob("gt_*.geojson"))) == 6


def test_export_skips_raster_projects(client, admin_user, operators, tiles, tmp_path):
    """Raster tiles in the DB must NOT appear in vector export output —
    the JOIN on projects.kind='vector' filters them out."""
    # The `tiles` fixture inserts 10 raster tiles. Add a vector project too.
    pid_v, _ = _seed(client, admin_user, tmp_path, count=2, name="mixed-v")
    # Classify a raster tile to give it a non-pending status.
    from tests.conftest import token
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get(
        "/api/tiles/next?project_id=1",
        headers={"Authorization": f"Bearer {op_tok}"},
    ).json()
    client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={"Authorization": f"Bearer {op_tok}",
                  "Content-Type": "application/octet-stream"},
        content=bytes([1]) * 65536,
    )
    out_dir = tmp_path / "mixed"
    _run_export([str(out_dir)])  # all projects
    # Only the 2 vector tiles should be there.
    assert len(list(out_dir.glob("gt_*.geojson"))) == 2


# ---- Edge: empty body skipped ---------------------------------------------

def test_export_skips_corrupt_body(client, admin_user, tmp_path):
    """A tile with malformed JSON in data_geojson is skipped (not crash)."""
    pid, ids = _seed(client, admin_user, tmp_path, count=2, name="cor")
    from backend.database import transaction
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "UPDATE tiles SET data_geojson='not json' WHERE id=?", (ids[0],),
        )
    out_dir = tmp_path / "corrupt"
    _run_export([str(out_dir), f"--project={pid}"])
    files = list(out_dir.glob("gt_*.geojson"))
    # Only the well-formed tile is exported.
    assert len(files) == 1
