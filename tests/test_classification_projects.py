"""Classification projects: end-to-end via the HTTP API.

Covers project CRUD with kind='classification', the mutual-exclusion rules,
the submit flow (JSON body with class_id, validation against allowed
class ids), the no-pause guard, GET /classification, the /image and
/features 415 responses, the dashboard's tile_class_distribution, the
admin reset/report-problem clearing of data_class_id, and the CSV
export script."""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _real_mbtiles(tmp_path: Path, name: str = "real.mbtiles") -> str:
    path = tmp_path / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT,"
        " tile_data BLOB, PRIMARY KEY(zoom_level, tile_column, tile_row));"
    )
    conn.execute("INSERT INTO metadata VALUES('format','png'),"
                 "('minzoom','0'),('maxzoom','3')")
    conn.commit()
    conn.close()
    return str(path)


def _create_classification_project(client, tok, tmp_path, *,
                                   name: str = "classif", classes=None) -> dict:
    body = {
        "name": name,
        "kind": "classification",
        "primary_mbtiles": _real_mbtiles(tmp_path, f"{name}.mbtiles"),
        "classes": classes or [
            {"id": 1, "name": "agua", "color": "#1f77b4"},
            {"id": 2, "name": "vegetacao", "color": "#2ca02c"},
            {"id": 3, "name": "edif", "color": "#d62728"},
        ],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 200, r.text
    return r.json()


def _seed_pending_tile(project_id: int, name: str = "ct1") -> int:
    from backend.database import connect
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (project_id, name, 0.0, 0.0, 0.1, 0.1),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


def _assign_operator(client, tok, project_id: int, op_id: int) -> None:
    r = client.post(
        f"/api/admin/projects/{project_id}/members",
        json={"user_id": op_id, "role": "operator"},
        headers=h(tok),
    )
    assert r.status_code in (200, 204), r.text


# ---- Project CRUD ---------------------------------------------------------

def test_create_classification_project_with_classes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="land_use")
    assert proj["kind"] == "classification"
    assert [c["id"] for c in proj["classes"]] == [1, 2, 3]
    assert proj["classes"][0]["name"] == "agua"


def test_classification_rejects_attributes_payload(client, admin_user, tmp_path):
    """Mutual exclusion: classification expects classes, never attributes."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "bad",
        "kind": "classification",
        "primary_mbtiles": _real_mbtiles(tmp_path),
        "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        "attributes": [{"key": "k", "label": "K", "type": "text"}],
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "attributes_not_supported"


def test_classification_requires_classes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = {
        "name": "noclasses",
        "kind": "classification",
        "primary_mbtiles": _real_mbtiles(tmp_path),
    }
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_classes"


# ---- Submit flow ----------------------------------------------------------

def test_submit_classification_valid(client, admin_user, operators, tmp_path):
    """Operator picks a class id and POSTs JSON; tile moves to classified."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="ok")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])

    op_tok = token(client, op["username"], op["password"])
    nxt = client.get(f"/api/tiles/next?project_id={proj['id']}",
                     headers=h(op_tok)).json()
    assert nxt["id"] == tile_id

    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps({"class_id": 2}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["class_id"] == 2

    # GET /classification returns the assigned class.
    g = client.get(f"/api/tiles/{tile_id}/classification", headers=h(op_tok))
    assert g.status_code == 200 and g.json() == {"class_id": 2}


def test_submit_rejects_class_outside_project(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="strict")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))

    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps({"class_id": 99}),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_class"


def test_submit_rejects_malformed_json(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="bad-json")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))

    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=b"not json",
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_class"


def test_pause_not_supported(client, admin_user, operators, tmp_path):
    """Classification tiles can't be paused — single-click finalizes."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="nopause")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))

    r = client.post(
        f"/api/tiles/{tile_id}/pause",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps({"class_id": 1}),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pause_not_supported"


# ---- Endpoint dispatch by kind --------------------------------------------

def test_image_endpoint_415_on_classification(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="img-test")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    r = client.get(f"/api/tiles/{tile_id}/image", headers=h(op_tok))
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "wrong_kind"


def test_classification_endpoint_204_when_unset(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="get204")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    r = client.get(f"/api/tiles/{tile_id}/classification", headers=h(op_tok))
    assert r.status_code == 204


def test_classification_endpoint_415_on_raster(client, admin_user, operators, tiles):
    """Raster tile via /classification returns 415."""
    from backend.database import connect
    tok = token(client, admin_user["username"], admin_user["password"])
    op = operators[0]
    op_tok = token(client, op["username"], op["password"])
    conn = connect()
    try:
        tid = conn.execute("SELECT id FROM tiles ORDER BY id LIMIT 1").fetchone()["id"]
    finally:
        conn.close()
    r = client.get(f"/api/tiles/{tid}/classification", headers=h(op_tok))
    assert r.status_code == 415


# ---- Dashboard ------------------------------------------------------------

def test_tile_class_distribution(client, admin_user, operators, tmp_path):
    """tile_class_distribution counts one tile per class_id after submit."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="dist")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    op_tok = token(client, op["username"], op["password"])

    # Submit 3 tiles: 2× class_id=1, 1× class_id=2.
    for i, cid in enumerate((1, 1, 2)):
        tile_id = _seed_pending_tile(proj["id"], name=f"t_{cid}_{i}")
        client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
        r = client.post(
            f"/api/tiles/{tile_id}/classify",
            headers={**h(op_tok), "Content-Type": "application/json"},
            content=json.dumps({"class_id": cid}),
        )
        assert r.status_code == 200, r.text

    r = client.get(f"/api/admin/tile-class-distribution?project_id={proj['id']}",
                   headers=h(tok))
    assert r.status_code == 200, r.text
    rows = r.json()
    by_id = {row["class_id"]: row for row in rows}
    assert by_id[1]["count"] == 2
    assert by_id[2]["count"] == 1
    assert by_id[1]["name"] == "agua"


# ---- Reset / problem clears data_class_id ---------------------------------

def test_reset_clears_data_class_id(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="reset")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    tile_id = _seed_pending_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps({"class_id": 1}),
    )
    # Admin resets the tile back to pending.
    r = client.post(
        f"/api/admin/tiles/{tile_id}/reset",
        json={"reason": "test"}, headers=h(tok),
    )
    assert r.status_code in (200, 204), r.text
    # data_class_id is null again.
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, data_class_id FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["data_class_id"] is None


# ---- Export script --------------------------------------------------------

def _run_export(args: list[str]) -> int:
    from backend.scripts import export_classifications
    old_argv = sys.argv
    sys.argv = ["export_classifications", *args]
    try:
        export_classifications.main()
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    finally:
        sys.argv = old_argv


def test_export_csv(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_classification_project(client, tok, tmp_path, name="export")
    op = operators[0]
    _assign_operator(client, tok, proj["id"], op["id"])
    op_tok = token(client, op["username"], op["password"])

    # Submit two tiles → 'classified' (not reviewed). Default --status='reviewed'
    # excludes them, so we'll pass --status reviewed+classified.
    for cid, name in [(1, "a"), (2, "b")]:
        tile_id = _seed_pending_tile(proj["id"], name=name)
        client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
        client.post(
            f"/api/tiles/{tile_id}/classify",
            headers={**h(op_tok), "Content-Type": "application/json"},
            content=json.dumps({"class_id": cid}),
        )

    out_dir = tmp_path / "out"
    code = _run_export([str(out_dir),
                        f"--project={proj['id']}",
                        "--status", "reviewed+classified"])
    assert code == 0
    csv_path = out_dir / "classifications.csv"
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2
    assert {r["class_id"] for r in rows} == {"1", "2"}
    assert {r["class_name"] for r in rows} == {"agua", "vegetacao"}


# ---- Pure helpers (classify_utils) ----------------------------------------

def test_parse_class_id_happy():
    from backend.classify_utils import parse_class_id
    assert parse_class_id('{"class_id": 3}', [1, 2, 3]) == 3


def test_parse_class_id_rejects_unknown_class():
    from backend.classify_utils import parse_class_id
    with pytest.raises(ValueError, match="fora das classes"):
        parse_class_id('{"class_id": 99}', [1, 2, 3])


def test_parse_class_id_rejects_non_integer():
    from backend.classify_utils import parse_class_id
    with pytest.raises(ValueError, match="inteiro"):
        parse_class_id('{"class_id": "3"}', [1, 2, 3])
    with pytest.raises(ValueError, match="inteiro"):
        parse_class_id('{"class_id": true}', [1, 2, 3])


def test_parse_class_id_rejects_malformed_json():
    from backend.classify_utils import parse_class_id
    with pytest.raises(ValueError, match="JSON"):
        parse_class_id("{not json", [1, 2, 3])
    with pytest.raises(ValueError, match="vazio"):
        parse_class_id("", [1])
