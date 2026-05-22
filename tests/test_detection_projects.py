"""Detection projects (kind=detection): box FeatureCollections, one class per
box, optional box_required gate. Mirrors the vector/classification suites."""
import json
import sqlite3

import pytest

from tests.conftest import token
from tests._vector_helpers import auth as h, make_real_mbtiles


# ---- helpers ---------------------------------------------------------------

def _detection_body(tmp_path, *, name="det", box_required=False, classes=None):
    return {
        "name": name,
        "kind": "detection",
        "box_required": box_required,
        "primary_mbtiles": make_real_mbtiles(tmp_path, f"{name}.mbtiles", maxzoom=3),
        "classes": classes or [
            {"id": 1, "name": "Carro", "color": "#e41a1c"},
            {"id": 2, "name": "Casa", "color": "#377eb8"},
        ],
    }


def _create(client, tok, tmp_path, **kw):
    r = client.post("/api/admin/projects", json=_detection_body(tmp_path, **kw), headers=h(tok))
    assert r.status_code == 200, r.text
    return r.json()


def _box(class_id, w, s, e, n):
    return {
        "type": "Feature",
        "properties": {"class_id": class_id},
        "geometry": {"type": "Polygon",
                     "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
    }


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _seed_tile(project_id, *, status="in_progress", assigned_to=None,
               bbox=(-50.0, -25.0, -49.99, -24.99)):
    from backend.database import connect
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status, assigned_to)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, "t1", *bbox, status, assigned_to),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


def _classify(client, tok, tile_id, fc, version=None):
    headers = {**h(tok), "Content-Type": "application/json"}
    if version is not None:
        headers["X-Tile-Version"] = str(version)
    return client.post(f"/api/tiles/{tile_id}/classify", headers=headers,
                       content=json.dumps(fc))


# ---- creation --------------------------------------------------------------

def test_create_detection_project(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, box_required=True)
    assert proj["kind"] == "detection"
    assert proj["box_required"] is True
    assert [c["name"] for c in proj["classes"]] == ["Carro", "Casa"]


def test_detection_rejects_attributes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _detection_body(tmp_path)
    body["attributes"] = [{"key": "x", "label": "X", "type": "text"}]
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "attributes_not_supported"


def test_detection_requires_classes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    body = _detection_body(tmp_path)
    body["classes"] = []
    r = client.post("/api/admin/projects", json=body, headers=h(tok))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_classes"


# ---- submit ----------------------------------------------------------------

def test_submit_boxes_classifies_and_caches_counts(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    body = _fc(_box(1, -50.0, -25.0, -49.995, -24.995),
               _box(1, -49.995, -24.995, -49.99, -24.99),
               _box(2, -49.998, -24.998, -49.99, -24.99))
    r = _classify(client, tok, tid, body)
    assert r.status_code == 200, r.text

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, feature_count, class_counts FROM tiles WHERE id=?", (tid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified"
    assert row["feature_count"] == 3
    assert json.loads(row["class_counts"]) == {"1": 2, "2": 1}


def test_box_required_rejects_empty(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, box_required=True)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    r = _classify(client, tok, tid, _fc())
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_boxes"


def test_empty_tile_valid_when_not_required(client, admin_user, tmp_path):
    """A negative sample (0 boxes) submits fine when box_required is off."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, box_required=False)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    r = _classify(client, tok, tid, _fc())
    assert r.status_code == 200, r.text
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status, feature_count FROM tiles WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "classified" and row["feature_count"] == 0


def test_submit_rejects_unknown_class(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    r = _classify(client, tok, tid, _fc(_box(99, -50.0, -25.0, -49.99, -24.99)))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_boxes"


def test_submit_rejects_non_rectangle(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    # A triangle (3 distinct longitudes) is not an axis-aligned rectangle.
    tri = {"type": "Feature", "properties": {"class_id": 1},
           "geometry": {"type": "Polygon",
                        "coordinates": [[[-50.0, -25.0], [-49.98, -25.0], [-49.99, -24.98], [-50.0, -25.0]]]}}
    r = _classify(client, tok, tid, _fc(tri))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_boxes"


# ---- pause -----------------------------------------------------------------

def test_pause_persists_partial_without_class_validation(client, admin_user, tmp_path):
    """Pause is a save-my-work affordance: a box with an out-of-palette class
    still round-trips (validation deferred to submit)."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    body = _fc(_box(99, -50.0, -25.0, -49.99, -24.99))  # bad class id
    r = client.post(f"/api/tiles/{tid}/pause",
                    headers={**h(tok), "Content-Type": "application/json"},
                    content=json.dumps(body))
    assert r.status_code == 200, r.text
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT paused_at, feature_count FROM tiles WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["paused_at"] is not None and row["feature_count"] == 1


# ---- read endpoints --------------------------------------------------------

def test_features_endpoint_serves_boxes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    _classify(client, tok, tid, _fc(_box(1, -50.0, -25.0, -49.99, -24.99)))
    r = client.get(f"/api/tiles/{tid}/features", headers=h(tok))
    assert r.status_code == 200
    assert len(r.json()["features"]) == 1


def test_image_endpoint_415_on_detection(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    r = client.get(f"/api/tiles/{tid}/image", headers=h(tok))
    assert r.status_code == 415


# ---- invariants ------------------------------------------------------------

def test_kind_is_immutable(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    r = client.patch(f"/api/admin/projects/{proj['id']}",
                     json={"kind": "raster"}, headers=h(tok))
    # kind is dropped from the allow-list silently; project stays detection.
    assert r.status_code in (200, 400)
    again = client.get(f"/api/projects/{proj['id']}", headers=h(tok)).json()
    assert again["kind"] == "detection"


def test_class_removal_blocked_with_tiles(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    _seed_tile(proj["id"])  # any tile
    r = client.put(f"/api/admin/projects/{proj['id']}/classes",
                   json={"classes": [{"id": 1, "name": "Carro", "color": "#e41a1c"}]},
                   headers=h(tok))
    assert r.status_code == 409


# ---- overlay ---------------------------------------------------------------

def test_overlay_renders_boxes(client, admin_user, tmp_path):
    """End-to-end overlay: classify a box, render the XYZ tile that contains it,
    assert the PNG carries a pixel in the box's class color. Locks the
    rasterization pipeline (lonlat→pixel + class lookup + ImageDraw rectangle)."""
    from PIL import Image
    from io import BytesIO
    from backend import mask_tile_service, project_service
    tok = token(client, admin_user["username"], admin_user["password"])
    # Bright magenta class so the assertion can't false-positive on background.
    proj = _create(client, tok, tmp_path, name="ovl",
                   classes=[{"id": 1, "name": "alvo", "color": "#ff00ff"},
                            {"id": 2, "name": "outro", "color": "#000000"}])
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"], bbox=(0.0, 0.0, 0.1, 0.1))
    # A box covering most of the tile so it's several pixels wide at z=10.
    _classify(client, tok, tid, _fc(_box(1, 0.01, 0.01, 0.09, 0.09)))

    z = 10
    x_min, y_min, _, _ = mask_tile_service.wm_tiles_for_bbox(z, 0.0, 0.0, 0.1, 0.1)
    png = mask_tile_service._render_detection_tile(
        proj["id"], z, x_min, y_min, project_service.get_project(proj["id"]),
    )
    assert png is not None, "overlay must not be empty when a box is classified"
    img = Image.open(BytesIO(png)).convert("RGBA")
    px = img.load()
    matched = any(
        px[x_, y_][0] > 200 and px[x_, y_][1] < 80 and px[x_, y_][2] > 200 and px[x_, y_][3] > 0
        for x_ in range(img.width) for y_ in range(img.height)
    )
    assert matched, "expected magenta box pixels in the rendered overlay"


def test_review_flow_persists_reviewer_boxes(client, admin_user, operators, tmp_path):
    """in_review → reviewed: the reviewer's overridden boxes replace the body
    and reviewed_by is recorded (classified_by preserved)."""
    from backend.database import connect
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, name="detrev")
    classifier = operators[0]["id"]
    # Seed an in_review tile assigned to admin, pre-classified by an operator.
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,assigned_to,classified_by,classified_at,data_geojson,feature_count)
               VALUES (?,?,?,?,?,?, 'in_review', ?, ?, datetime('now'), ?, 1)""",
            (proj["id"], "rev1", -50.0, -25.0, -49.99, -24.99, admin_user["id"], classifier,
             json.dumps(_fc(_box(1, -50.0, -25.0, -49.99, -24.99)))),
        )
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    # Reviewer overrides with two boxes.
    new = _fc(_box(2, -50.0, -25.0, -49.995, -24.995), _box(1, -49.995, -24.995, -49.99, -24.99))
    r = client.post(f"/api/tiles/{tid}/review",
                    headers={**h(tok), "Content-Type": "application/json"}, content=json.dumps(new))
    assert r.status_code == 200, r.text
    conn = connect()
    try:
        row = conn.execute("SELECT status, reviewed_by, classified_by, feature_count, class_counts, data_geojson FROM tiles WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "reviewed"
    assert row["reviewed_by"] == admin_user["id"]
    assert row["classified_by"] == operators[0]["id"]  # preserved
    assert row["feature_count"] == 2
    assert json.loads(row["class_counts"]) == {"2": 1, "1": 1}


def test_review_rejects_unknown_class(client, admin_user, tmp_path):
    """The review submit branch re-validates class_id against the palette."""
    from backend.database import connect
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, name="detrev2")
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,assigned_to,data_geojson)
               VALUES (?,?,?,?,?,?, 'in_review', ?, ?)""",
            (proj["id"], "rev2", -50.0, -25.0, -49.99, -24.99, admin_user["id"],
             json.dumps(_fc(_box(1, -50.0, -25.0, -49.99, -24.99)))),
        )
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    r = client.post(f"/api/tiles/{tid}/review",
                    headers={**h(tok), "Content-Type": "application/json"},
                    content=json.dumps(_fc(_box(99, -50.0, -25.0, -49.99, -24.99))))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_boxes"


def test_submit_rejects_degenerate_box(client, admin_user, tmp_path):
    """A zero-area box (w==e) collapses to one distinct longitude → rejected."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    degenerate = {"type": "Feature", "properties": {"class_id": 1},
                  "geometry": {"type": "Polygon",
                               "coordinates": [[[-50.0, -25.0], [-50.0, -25.0], [-50.0, -24.99], [-50.0, -24.99], [-50.0, -25.0]]]}}
    r = _classify(client, tok, tid, _fc(degenerate))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_boxes"


def test_submit_rejects_non_polygon_geometry(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    line = {"type": "Feature", "properties": {"class_id": 1},
            "geometry": {"type": "LineString", "coordinates": [[-50.0, -25.0], [-49.99, -24.99]]}}
    r = _classify(client, tok, tid, _fc(line))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_boxes"


def test_report_problem_clears_geojson(client, admin_user, tmp_path):
    """report_problem must wipe data_geojson so a later kind change can't leak
    a stale body (CLAUDE.md invariant)."""
    from backend.database import connect
    from backend.database import connect
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    # Seed an in_progress tile assigned to admin with a box body, then report.
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,
                                 status,assigned_to,data_geojson,feature_count)
               VALUES (?,?,?,?,?,?, 'in_progress', ?, ?, 1)""",
            (proj["id"], "prob", -50.0, -25.0, -49.99, -24.99, admin_user["id"],
             json.dumps(_fc(_box(1, -50.0, -25.0, -49.99, -24.99)))),
        )
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    r = client.post(f"/api/tiles/{tid}/report-problem", json={"note": "ruim"}, headers=h(tok))
    assert r.status_code == 200, r.text
    conn = connect()
    try:
        row = conn.execute("SELECT status, data_geojson FROM tiles WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "problem"
    assert row["data_geojson"] is None


def test_box_required_accepts_nonempty(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, box_required=True)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    r = _classify(client, tok, tid, _fc(_box(1, -50.0, -25.0, -49.99, -24.99)))
    assert r.status_code == 200, r.text


def test_box_required_patch_toggle_revalidates(client, admin_user, tmp_path):
    """Flipping box_required via PATCH changes the empty-submit verdict."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, box_required=False)
    # Empty submit accepted while off.
    t1 = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    assert _classify(client, tok, t1, _fc()).status_code == 200
    # Turn it on; a fresh empty submit is now rejected.
    pr = client.patch(f"/api/admin/projects/{proj['id']}", json={"box_required": True}, headers=h(tok))
    assert pr.status_code == 200, pr.text
    assert client.get(f"/api/projects/{proj['id']}", headers=h(tok)).json()["box_required"] is True
    t2 = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    assert _classify(client, tok, t2, _fc()).status_code == 422


def test_submit_rejects_huge_body(client, admin_user, tmp_path):
    """Bodies past the JSON cap are refused (413), not parsed."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path)
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    huge = "x" * 1_600_000  # > 1.5MB content-length gate
    r = client.post(f"/api/tiles/{tid}/classify",
                    headers={**h(tok), "Content-Type": "application/json"}, content=huge)
    assert r.status_code == 413


# ---- export ----------------------------------------------------------------

def test_export_detections_geojson(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create(client, tok, tmp_path, name="detexp")
    tid = _seed_tile(proj["id"], assigned_to=admin_user["id"])
    _classify(client, tok, tid, _fc(_box(1, -50.0, -25.0, -49.99, -24.99)))
    # Promote to reviewed so the default export filter picks it up.
    from backend.database import connect
    conn = connect()
    try:
        conn.execute("UPDATE tiles SET status='reviewed', reviewed_at=datetime('now') WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    out = tmp_path / "exp"
    import sys
    from backend.scripts import export_detections
    old = sys.argv
    sys.argv = ["export_detections", str(out), "--project", "detexp"]
    try:
        export_detections.main()
    finally:
        sys.argv = old
    files = list(out.glob("gt_*.geojson"))
    assert len(files) == 1
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["type"] == "FeatureCollection"
    assert doc["features"][0]["geometry"]["type"] == "Polygon"
    assert (out / "manifest.csv").exists()


# ---- detection_utils (pure) ------------------------------------------------

def test_detection_utils_validate():
    from backend import detection_utils as du
    good = json.dumps(_fc(_box(1, 0, 0, 1, 1)))
    ok, payload = du.validate_submission(good, [1, 2], box_required=False)
    assert ok and payload["doc"]["features"]
    ok, payload = du.validate_submission(json.dumps(_fc()), [1], box_required=True)
    assert not ok and "ao menos uma caixa" in payload["errors"][0]
    assert du.class_counts(_fc(_box(1, 0, 0, 1, 1), _box(1, 0, 0, 2, 2))) == {"1": 2}
