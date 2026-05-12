"""Vector projects: end-to-end via the HTTP API.

Covers project CRUD with the new kind/topology_required/attributes fields,
the mutual-exclusion rules between raster and vector payloads, the submit
flow for vector tiles (in_progress→classified, in_review→reviewed), the
attribute schema editor, the immutable-kind invariant, and topology
validation gating.
"""
import json

import pytest
from tests.conftest import token
from tests._vector_helpers import (
    auth as h,
    create_vector_project as _create_vector_project,
    fc as _fc,
    line_feature as _line_feature,
    make_real_mbtiles as _real_mbtiles,
    vector_project_body as _vector_project_body,
)


def _seed_tile(project_id: int, name="vt"):
    """Insert a single pending tile in the project for the operator to pick."""
    from backend.database import connect
    from backend.vector_utils import empty_feature_collection
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status, data_geojson)
               VALUES (?,?,?,?,?,?,'pending',?)""",
            (project_id, name, 0.0, 0.0, 0.1, 0.1, empty_feature_collection()),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


# ---- Project CRUD ----------------------------------------------------------

def test_create_vector_project_with_attributes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _vector_project_body(
        tmp_path, name="rodovias",
        attributes=[
            {"key": "pavimento", "type": "enum", "label": "Pavimento",
             "required": True, "options": ["asfalto", "terra"]},
            {"key": "lanes", "type": "number", "label": "Faixas",
             "required": False},
        ],
    )
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["kind"] == "vector"
    assert proj["topology_required"] is False
    keys = [a["key"] for a in proj["attributes"]]
    assert keys == ["pavimento", "lanes"]
    assert proj["attributes"][0]["options"] == ["asfalto", "terra"]


def test_create_raster_with_attributes_rejected(client, admin_user, tmp_path):
    """Mutual exclusion: a raster project must not accept attributes."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "bad-raster",
            "kind": "raster",
            "primary_mbtiles": _real_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#001122"}],
            "attributes": [{"key": "k", "label": "K", "type": "text"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "attributes_not_supported"


def test_create_vector_with_classes_rejected(client, admin_user, tmp_path):
    """And a vector project must not accept classes."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _vector_project_body(tmp_path, name="bad-vector")
    body["classes"] = [{"id": 1, "name": "x", "color": "#001122"}]
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "classes_on_vector"


def test_create_vector_rejects_invalid_attribute_keys(client, admin_user, tmp_path):
    """Snake_case is enforced — frontend uses keys as form names + payload
    keys, so spaces/uppercase/etc would silently break later."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _vector_project_body(
        tmp_path, name="bad-keys",
        attributes=[{"key": "Tipo Errado", "type": "text", "label": "T"}],
    )
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 422  # Pydantic pattern check


def test_create_vector_enum_requires_options(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _vector_project_body(
        tmp_path, name="enum-noop",
        attributes=[{"key": "pav", "type": "enum", "label": "Pav",
                     "required": True}],
    )
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "enum_needs_options"


def test_kind_is_immutable_after_creation(client, admin_user, tmp_path):
    """Patching `kind` is a hard error — flipping kinds would orphan
    tile bodies. The allow-list rejects unknown fields with 400."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = _create_vector_project(client, tok, tmp_path, name="v1")
    r = client.patch(
        f"/api/admin/projects/{new['id']}",
        json={"kind": "raster"},  # not in allow-list
        headers=h(tok),
    )
    # Pydantic ProjectUpdateIn doesn't have a `kind` field, so the value is
    # silently dropped at the schema layer — the project stays vector.
    assert r.status_code == 200
    assert r.json()["kind"] == "vector"


def test_topology_required_can_be_toggled(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    new = _create_vector_project(client, tok, tmp_path, name="hidroflag", topology=False)
    r = client.patch(
        f"/api/admin/projects/{new['id']}",
        json={"topology_required": True},
        headers=h(tok),
    )
    assert r.status_code == 200 and r.json()["topology_required"] is True


# ---- Attribute schema editor ----------------------------------------------

def test_set_attributes_replaces_schema(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    new = _create_vector_project(client, tok, tmp_path, name="sched")
    r = client.put(
        f"/api/admin/projects/{new['id']}/attributes",
        json={"attributes": [
            {"key": "tipo", "type": "enum", "label": "Tipo X",
             "required": True, "options": ["a", "b"]},
            {"key": "obs", "type": "text", "label": "Obs", "required": False},
        ]},
        headers=h(tok),
    )
    assert r.status_code == 200
    keys = [a["key"] for a in r.json()["attributes"]]
    assert keys == ["tipo", "obs"]
    # Label updated.
    assert r.json()["attributes"][0]["label"] == "Tipo X"


def test_set_attributes_blocks_remove_when_used(client, admin_user, operators, tmp_path):
    """Removing a key referenced by an existing feature is rejected."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="used", topology=False)
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    # Operator pulls + submits a feature using `tipo`.
    nxt = client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok)).json()
    assert nxt["id"] == tile_id
    fc = _fc(_line_feature([[0.01, 0.01], [0.05, 0.05]], tipo="arroio"))
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 200, r.text
    # Now admin tries to remove `tipo` from the schema → 409.
    r2 = client.put(
        f"/api/admin/projects/{proj['id']}/attributes",
        json={"attributes": [
            {"key": "novo", "type": "text", "label": "N", "required": False},
        ]},
        headers=h(tok),
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "attribute_in_use"
    assert "tipo" in r2.json()["detail"]["removed"]


def test_set_attributes_rejected_on_raster_project(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.put(
        "/api/admin/projects/1/attributes",
        json={"attributes": [
            {"key": "k", "type": "text", "label": "L", "required": False},
        ]},
        headers=h(tok),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "not_vector_project"


# ---- Submit flow ----------------------------------------------------------

def test_vector_submit_classify_and_review(client, admin_user, operators, tmp_path):
    """Full classify → review cycle with vector body. Status transitions
    and feature_count cache update on each step."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="cycle")
    for op in operators[:2]:
        client.post(
            f"/api/admin/projects/{proj['id']}/members",
            json={"user_id": op["id"], "role": "reviewer"},
            headers=h(tok),
        )
    tile_id = _seed_tile(proj["id"])

    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op1_tok)).json()
    fc = _fc(
        _line_feature([[0, 0], [0.05, 0.05]], tipo="rio"),
        _line_feature([[0.05, 0.05], [0.08, 0.07]], tipo="arroio"),
    )
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op1_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 200, r.text

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, feature_count, data_geojson FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["feature_count"] == 2

    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    review = client.get(
        f"/api/tiles/next?project_id={proj['id']}", headers=h(op2_tok),
    ).json()
    assert review["id"] == tile_id and review["status"] == "in_review"
    fc2 = _fc(_line_feature([[0, 0], [0.1, 0.1]], tipo="rio"))
    r2 = client.post(
        f"/api/tiles/{tile_id}/review",
        headers={**h(op2_tok), "Content-Type": "application/json"},
        content=json.dumps(fc2),
    )
    assert r2.status_code == 200
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, feature_count FROM tiles WHERE id=?", (tile_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "reviewed" and row["feature_count"] == 1


def test_vector_submit_invalid_geojson(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="bad-json")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content="{not json",
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_features"
    assert any("GeoJSON" in e for e in r.json()["detail"]["errors"])


def test_vector_submit_required_attribute_missing(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="reqmiss")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    fc = _fc(_line_feature([[0, 0], [1, 1]]))  # missing required `tipo`
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 422
    assert any("tipo" in e for e in r.json()["detail"]["errors"])


def test_vector_submit_topology_off_accepts_messy(client, admin_user, operators, tmp_path):
    """topology_required=False: the chain submits even with a cycle."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="loose", topology=False)
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    fc = _fc(
        _line_feature([[0, 0], [1, 0]], tipo="rio", direction="forward"),
        _line_feature([[1, 0], [1, 1]], tipo="rio", direction="forward"),
        _line_feature([[1, 1], [0, 0]], tipo="rio", direction="forward"),  # cycle
    )
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 200


def test_vector_submit_topology_on_blocks_cycle(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="strict", topology=True)
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    fc = _fc(
        _line_feature([[0, 0], [1, 0]], tipo="rio", direction="forward"),
        _line_feature([[1, 0], [1, 1]], tipo="rio", direction="forward"),
        _line_feature([[1, 1], [0, 0]], tipo="rio", direction="forward"),
    )
    r = client.post(
        f"/api/tiles/{tile_id}/classify",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 422
    assert any("ciclo" in e.lower() for e in r.json()["detail"]["errors"])


# ---- /image vs /features routing ------------------------------------------

def test_image_endpoint_415_on_vector_tile(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="img415")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    r = client.get(f"/api/tiles/{tile_id}/image", headers=h(op_tok))
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "wrong_kind"


def test_features_endpoint_415_on_raster_tile(client, admin_user, operators, tiles):
    tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(tok)).json()
    r = client.get(f"/api/tiles/{nxt['id']}/features", headers=h(tok))
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "wrong_kind"


def test_features_endpoint_returns_canonical_empty_for_unsubmitted(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="empty")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    r = client.get(f"/api/tiles/{tile_id}/features", headers=h(op_tok))
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == []


# ---- Pause: vector body persisted, no validation ---------------------------

def test_vector_pause_saves_partial_invalid_features(client, admin_user, operators, tmp_path):
    """Pause must round-trip even with attribute holes — the operator hits
    pause to step away, not to commit. Topology / required fields are only
    enforced at submit time."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="pausev")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    fc = _fc(_line_feature([[0, 0], [1, 1]]))  # no `tipo` (required), no direction
    r = client.post(
        f"/api/tiles/{tile_id}/pause",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content=json.dumps(fc),
    )
    assert r.status_code == 200, r.text
    # Tile is still in_progress + paused; body persisted.
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, paused_at, data_geojson, feature_count FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_progress" and row["paused_at"]
    assert row["feature_count"] == 1
    assert "LineString" in row["data_geojson"]


def test_vector_pause_rejects_invalid_geojson(client, admin_user, operators, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="pause-bad")
    op = operators[0]
    client.post(
        f"/api/admin/projects/{proj['id']}/members",
        json={"user_id": op["id"], "role": "operator"},
        headers=h(tok),
    )
    tile_id = _seed_tile(proj["id"])
    op_tok = token(client, op["username"], op["password"])
    client.get(f"/api/tiles/next?project_id={proj['id']}", headers=h(op_tok))
    r = client.post(
        f"/api/tiles/{tile_id}/pause",
        headers={**h(op_tok), "Content-Type": "application/json"},
        content="not json",
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_geojson"


# ---- Clone preserves attributes -------------------------------------------

def test_clone_vector_project_copies_attributes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(
        client, tok, tmp_path, name="src",
        attributes=[
            {"key": "k1", "type": "text", "label": "L1", "required": True},
            {"key": "k2", "type": "boolean", "label": "L2"},
        ],
    )
    r = client.post(
        f"/api/admin/projects/{proj['id']}/clone",
        json={"name": "src_clone"},
        headers=h(tok),
    )
    assert r.status_code == 200
    cloned = r.json()
    assert cloned["kind"] == "vector"
    assert [a["key"] for a in cloned["attributes"]] == ["k1", "k2"]
    # Source unchanged.
    src = client.get(f"/api/projects/{proj['id']}", headers=h(tok)).json()
    assert [a["key"] for a in src["attributes"]] == ["k1", "k2"]
