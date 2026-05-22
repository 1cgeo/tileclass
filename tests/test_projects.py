"""Project CRUD + membership + class management."""
import sqlite3
import pytest
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _stub_mbtiles(tmp_path, name="stub.mbtiles") -> str:
    """Drop a placeholder file. Path validation only checks existence — the
    mbtiles reader is not opened by create/update."""
    p = tmp_path / name
    p.write_bytes(b"\x00")
    return str(p)


def _real_mbtiles(tmp_path, name="real.mbtiles", *, fmt="png",
                  tile_bytes=b"\x89PNG\r\n\x1a\n") -> str:
    """Materialise a minimal but valid mbtiles file the reader pool can open
    and answer get_tile() from. Single tile at z=0,x=0,y=0."""
    path = tmp_path / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB,"
        " PRIMARY KEY(zoom_level, tile_column, tile_row));"
    )
    conn.execute("INSERT INTO metadata VALUES('format',?)", (fmt,))
    conn.execute("INSERT INTO metadata VALUES('minzoom','0'),('maxzoom','3')")
    conn.execute(
        "INSERT INTO tiles VALUES(0, 0, 0, ?)", (tile_bytes,)
    )
    conn.commit()
    conn.close()
    return str(path)


# ---- Read paths -------------------------------------------------------------

def test_list_projects_returns_default(client, admin_user):
    """Fresh DB has the seed default project; admin sees it."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/projects", headers=h(tok))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "default"
    assert body[0]["role"] == "admin"


def test_get_project_includes_classes_and_layers(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/projects/1", headers=h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "default"
    assert isinstance(body["classes"], list) and len(body["classes"]) >= 1
    assert all({"id", "name", "color"} <= set(c) for c in body["classes"])
    layers = body["layers"]
    assert set(layers.keys()) == {
        "primary", "secondary", "tertiary", "ref_primary", "ref_secondary"
    }
    # Each layer is None (no path configured / file missing without admin
    # awareness yet) or a dict with a populated `url`/`error`. The default
    # config.yaml's mbtiles paths point at data_external/ which doesn't ship
    # in the repo, so all layers may be None — assertion stays loose to
    # accommodate dev machines that have or don't have those files.
    for v in layers.values():
        if v is None:
            continue
        assert isinstance(v, dict)
        assert "url" in v or "error" in v


def test_member_sees_project_non_member_does_not(client, admin_user, operators, tmp_path):
    """A user listed as project member sees the project; an outsider gets 403."""
    admin_tok = token(client, admin_user["username"], admin_user["password"])
    # Create a second project and make only operators[0] a member.
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "secondary-proj",
            "primary_mbtiles": _stub_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(admin_tok),
    )
    assert new.status_code == 200, new.text
    pid = new.json()["id"]
    # Add op1 only.
    r = client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": operators[0]["id"], "role": "operator"},
        headers=h(admin_tok),
    )
    assert r.status_code == 200, r.text

    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])

    # Member can read.
    r1 = client.get(f"/api/projects/{pid}", headers=h(op1_tok))
    assert r1.status_code == 200
    assert r1.json()["role"] == "operator"
    # Non-member is rejected with project-specific error.
    r2 = client.get(f"/api/projects/{pid}", headers=h(op2_tok))
    assert r2.status_code == 403
    assert r2.json()["detail"]["error"] == "not_project_member"

    # Listing reflects membership: op1 sees default + new; op2 sees only default.
    list1 = client.get("/api/projects", headers=h(op1_tok)).json()
    list2 = client.get("/api/projects", headers=h(op2_tok)).json()
    assert {p["name"] for p in list1} == {"default", "secondary-proj"}
    assert {p["name"] for p in list2} == {"default"}


# ---- Create / validation ----------------------------------------------------

def test_create_project_requires_admin(client, admin_user, operators, tmp_path):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "x",
            "primary_mbtiles": _stub_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(op_tok),
    )
    assert r.status_code == 403


def test_create_rejects_missing_mbtiles(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "ghost",
            "primary_mbtiles": "/nonexistent/path/foo.mbtiles",
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "mbtiles_not_found"


def test_create_rejects_duplicate_name(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "default",  # already taken by seed
            "primary_mbtiles": _stub_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "name_taken"


@pytest.mark.parametrize("bad_classes,label", [
    ([{"id": 1, "name": "a", "color": "#aabbcc"}, {"id": 1, "name": "dup", "color": "#aabbcc"}], "duplicate id"),
    ([{"id": 1, "name": "a", "color": "not-a-hex"}], "bad color"),
    ([{"id": 1, "name": "", "color": "#aabbcc"}], "empty name"),
    ([{"id": 255, "name": "a", "color": "#aabbcc"}], "id out of mask byte range (>254)"),
    ([{"id": 0, "name": "a", "color": "#aabbcc"}], "id 0 reserved"),
])
def test_create_rejects_invalid_classes(client, admin_user, tmp_path, bad_classes, label):
    """Each malformed class set is rejected (4xx), not silently accepted —
    id range matters because class ids become raw mask bytes."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={"name": f"broken-{label[:6]}", "primary_mbtiles": _stub_mbtiles(tmp_path),
              "classes": bad_classes},
        headers=h(tok),
    )
    assert r.status_code in (400, 422), f"{label} should be rejected, got {r.status_code}"


def test_add_class_allowed_while_tiles_exist(client, admin_user, tiles):
    """Removing a class with tiles present is blocked (mask bytes may reference
    it), but ADDING a new class id is always allowed."""
    tok = token(client, admin_user["username"], admin_user["password"])
    # Default seed has classes 1..6; add a 7th, keeping all existing ones.
    new_classes = [{"id": i, "name": f"c{i}", "color": "#377eb8"} for i in range(1, 7)]
    new_classes.append({"id": 7, "name": "nova", "color": "#123456"})
    r = client.put("/api/admin/projects/1/classes", json={"classes": new_classes}, headers=h(tok))
    assert r.status_code == 200, r.text
    got = client.get("/api/projects/1", headers=h(tok)).json()["classes"]
    assert {c["id"] for c in got} == set(range(1, 8))


# ---- Update -----------------------------------------------------------------

def test_update_project_patches_only_given_fields(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.patch(
        "/api/admin/projects/1",
        json={"description": "novo texto", "mask_complete_required": False},
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "novo texto"
    assert body["mask_complete_required"] is False
    # Untouched fields preserved.
    assert body["name"] == "default"


def test_update_rejects_unknown_field(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    # Pydantic strips unknown fields by default; exercise the service directly
    # to prove the explicit allow-list is honoured.
    from backend import project_service
    with pytest.raises(Exception):
        project_service.update_project(1, fields={"bogus": 1}, updated_by=admin_user["id"])


# ---- Classes ----------------------------------------------------------------

def test_set_classes_can_rename_and_recolor(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    new_classes = [
        {"id": 1, "name": "agua_renomeada", "color": "#001122"},
        {"id": 2, "name": "edif", "color": "#aaaaaa"},
        {"id": 3, "name": "floresta", "color": "#33aa33"},
        {"id": 4, "name": "campo", "color": "#dddd00"},
        {"id": 5, "name": "cultivo", "color": "#aa00aa"},
        {"id": 6, "name": "terr", "color": "#ff8800"},
    ]
    r = client.put(
        "/api/admin/projects/1/classes",
        json={"classes": new_classes},
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    names = {c["id"]: c["name"] for c in body["classes"]}
    assert names[1] == "agua_renomeada"
    colors = {c["id"]: c["color"] for c in body["classes"]}
    assert colors[1] == "#001122"


def test_set_classes_blocks_removal_when_tiles_exist(client, admin_user, tiles):
    """`tiles` fixture inserts 10 tiles in the default project; removing any
    class must be rejected because mask bytes can reference removed ids."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.put(
        "/api/admin/projects/1/classes",
        json={"classes": [{"id": 1, "name": "agua", "color": "#000000"}]},  # drops 2..6
        headers=h(tok),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "class_in_use"


# ---- Members ----------------------------------------------------------------

def test_add_remove_member(client, admin_user, operators):
    tok = token(client, admin_user["username"], admin_user["password"])
    op = operators[0]
    # add
    r = client.post(
        "/api/admin/projects/1/members",
        json={"user_id": op["id"], "role": "reviewer"},
        headers=h(tok),
    )
    assert r.status_code == 200
    # listing includes them
    listed = client.get("/api/admin/projects/1/members", headers=h(tok)).json()
    assert any(m["id"] == op["id"] and m["project_role"] == "reviewer" for m in listed)
    # remove
    r = client.delete(f"/api/admin/projects/1/members/{op['id']}", headers=h(tok))
    assert r.status_code == 200
    # listing no longer shows them
    listed = client.get("/api/admin/projects/1/members", headers=h(tok)).json()
    assert not any(m["id"] == op["id"] for m in listed)


def test_member_endpoints_require_admin(client, admin_user, operators):
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    r = client.post(
        "/api/admin/projects/1/members",
        json={"user_id": operators[1]["id"], "role": "operator"},
        headers=h(op_tok),
    )
    assert r.status_code == 403


# ---- Delete -----------------------------------------------------------------

def test_delete_empty_project(client, admin_user, tmp_path):
    """A project with no tiles can be hard-deleted; classes and members are
    cascaded by the schema and the project disappears from the list."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "to-delete",
            "primary_mbtiles": _stub_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#000000"}],
        },
        headers=h(tok),
    ).json()
    pid = new["id"]
    r = client.delete(f"/api/admin/projects/{pid}", headers=h(tok))
    assert r.status_code == 200
    # Now gone.
    assert client.get(f"/api/projects/{pid}", headers=h(tok)).status_code == 404


def test_delete_project_with_tiles_blocked(client, admin_user, tiles):
    """Default project has 10 tiles after the fixture; deleting must 409."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.delete("/api/admin/projects/1", headers=h(tok))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "project_has_tiles"
    assert r.json()["detail"]["tile_count"] == 10


# ---- Inactive project blocks distribution ----------------------------------

def test_inactive_project_blocks_next(client, admin_user, operators, tiles):
    """Setting active=False stops /next from handing out tiles, even though
    membership and tiles are intact. Existing tiles can still be inspected
    (queue-stats, dashboard) for reporting."""
    adm = token(client, admin_user["username"], admin_user["password"])
    op = operators[0]
    op_tok = token(client, op["username"], op["password"])

    # Sanity: /next works while active.
    r = client.get("/api/tiles/next?project_id=1", headers=h(op_tok))
    assert r.status_code == 200

    # Disable the project.
    client.patch("/api/admin/projects/1", json={"active": False}, headers=h(adm))

    # /next is now blocked with a project-specific error code.
    r = client.get("/api/tiles/next?project_id=1", headers=h(op_tok))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "project_inactive"

    # Read-only queue stats keep working — admins still need the totals.
    r = client.get("/api/tiles/queue-stats?project_id=1", headers=h(op_tok))
    assert r.status_code == 200


# ---- XYZ endpoint -----------------------------------------------------------

def test_xyz_serves_bytes_for_member(client, admin_user, operators, tmp_path):
    """GET /api/projects/{id}/xyz/primary/{z}/{x}/{y}.png returns the tile
    bytes stored in the project's primary mbtiles."""
    admin_tok = token(client, admin_user["username"], admin_user["password"])
    mb = _real_mbtiles(tmp_path)
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "with-mbtiles",
            "primary_mbtiles": mb,
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(admin_tok),
    ).json()
    pid = new["id"]
    # Make op1 a member.
    client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": operators[0]["id"], "role": "operator"},
        headers=h(admin_tok),
    )
    op_tok = token(client, operators[0]["username"], operators[0]["password"])

    r = client.get(f"/api/projects/{pid}/xyz/primary/0/0/0.png", headers=h(op_tok))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # Body matches the bytes we seeded.
    assert r.content.startswith(b"\x89PNG")


def test_xyz_404_when_layer_unconfigured(client, admin_user, tmp_path):
    """Asking for a layer the project hasn't set up returns 404."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "primary-only",
            "primary_mbtiles": _real_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    ).json()
    pid = new["id"]
    r = client.get(f"/api/projects/{pid}/xyz/secondary/0/0/0.png", headers=h(tok))
    assert r.status_code == 404


def test_xyz_403_for_non_member(client, admin_user, operators, tmp_path):
    """Operator who isn't a member of the project cannot fetch its layers
    even if they're authenticated."""
    admin_tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "isolated",
            "primary_mbtiles": _real_mbtiles(tmp_path),
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(admin_tok),
    ).json()
    pid = new["id"]
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    r = client.get(f"/api/projects/{pid}/xyz/primary/0/0/0.png", headers=h(op_tok))
    assert r.status_code == 403


def test_clone_project_copies_config_and_classes(client, admin_user, operators, tmp_path):
    """Cloning duplicates paths + classes + flags into a new project; the
    name auto-suffixes when not provided. Memberships are NOT copied."""
    tok = token(client, admin_user["username"], admin_user["password"])
    # Add op1 as a member of the source so we can prove the clone doesn't
    # inherit memberships.
    client.post(
        "/api/admin/projects/1/members",
        json={"user_id": operators[0]["id"], "role": "reviewer"},
        headers=h(tok),
    )
    r = client.post(
        "/api/admin/projects/1/clone",
        json={"name": "default_clone"},
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["name"] == "default_clone"
    assert new["mask_complete_required"] is True
    # Classes copied byte-for-byte.
    src = client.get("/api/projects/1", headers=h(tok)).json()
    assert {(c["id"], c["name"], c["color"]) for c in new["classes"]} \
        == {(c["id"], c["name"], c["color"]) for c in src["classes"]}
    # Memberships NOT copied — admin must explicitly add.
    members = client.get(
        f"/api/admin/projects/{new['id']}/members", headers=h(tok)
    ).json()
    assert all(m["id"] != operators[0]["id"] for m in members)


def test_clone_default_name_suffix(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post("/api/admin/projects/1/clone", json={}, headers=h(tok))
    assert r.status_code == 200
    assert r.json()["name"] == "default_copia"


def test_clone_rejects_duplicate_name(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects/1/clone",
        json={"name": "default"},  # already taken
        headers=h(tok),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "name_taken"


def test_remote_url_accepted_as_layer(client, admin_user):
    """A Martin / TileServer-GL URL is a valid layer source; no file check."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "remote",
            "primary_mbtiles": "https://martin.example.com/sat/{z}/{x}/{y}.webp",
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    body = client.get(f"/api/projects/{pid}", headers=h(tok)).json()
    primary = body["layers"]["primary"]
    assert primary["url"] == "https://martin.example.com/sat/{z}/{x}/{y}.webp"
    assert primary["remote"] is True
    assert primary["ext"] == "webp"


def test_is_remote_layer_recognizes_schemes():
    """Pure check on the single source of truth for remote-vs-file layers."""
    from backend.project_service import is_remote_layer
    assert is_remote_layer("https://h/{z}/{x}/{y}.png") is True
    assert is_remote_layer("http://h/{z}/{x}/{y}.png") is True
    assert is_remote_layer("bingmaps://{z}/{x}/{y}") is True
    assert is_remote_layer("../data_external/tiles.mbtiles") is False
    assert is_remote_layer("/abs/path.mbtiles") is False
    assert is_remote_layer(None) is False
    assert is_remote_layer("") is False


def test_bingmaps_url_accepted_as_layer(client, admin_user):
    """bingmaps:// is a remote scheme (frontend rewrites it to Bing quadkeys),
    so it passes validation and reaches the editor as a pass-through URL."""
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "bing",
            "primary_mbtiles": "https://martin.example.com/sat/{z}/{x}/{y}.webp",
            "tertiary_mbtiles": "bingmaps://{z}/{x}/{y}",
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    tertiary = client.get(f"/api/projects/{pid}", headers=h(tok)).json()["layers"]["tertiary"]
    assert tertiary["remote"] is True
    assert tertiary["url"] == "bingmaps://{z}/{x}/{y}"


def test_remote_url_rejects_missing_placeholders(client, admin_user):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "broken-url",
            "primary_mbtiles": "https://martin.example.com/sat/0/0/0.png",
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "missing_url_placeholders"
    assert set(detail["missing"]) == {"{z}", "{x}", "{y}"}


def test_remote_layer_xyz_endpoint_returns_404(client, admin_user):
    """The proxied /xyz/{layer} endpoint only serves mbtiles. Remote-URL
    layers are fetched by MapLibre directly, so the proxy 404s."""
    tok = token(client, admin_user["username"], admin_user["password"])
    pid = client.post(
        "/api/admin/projects",
        json={
            "name": "remote-xyz",
            "primary_mbtiles": "https://martin.example.com/sat/{z}/{x}/{y}.png",
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    ).json()["id"]
    r = client.get(f"/api/projects/{pid}/xyz/primary/0/0/0.png", headers=h(tok))
    assert r.status_code == 404


def test_xyz_extension_must_match_format(client, admin_user, tmp_path):
    """An mbtiles whose metadata says format=png must reject .webp requests."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "format-check",
            "primary_mbtiles": _real_mbtiles(tmp_path, fmt="png"),
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    ).json()
    pid = new["id"]
    r = client.get(f"/api/projects/{pid}/xyz/primary/0/0/0.webp", headers=h(tok))
    assert r.status_code == 404


def test_layers_url_in_get_project_uses_real_format(client, admin_user, tmp_path):
    """GET /api/projects/{id} reports layers with `url`/`ext` derived from
    the actually-opened reader's metadata."""
    tok = token(client, admin_user["username"], admin_user["password"])
    new = client.post(
        "/api/admin/projects",
        json={
            "name": "layers-meta",
            "primary_mbtiles": _real_mbtiles(tmp_path, fmt="png"),
            "classes": [{"id": 1, "name": "x", "color": "#112233"}],
        },
        headers=h(tok),
    ).json()
    pid = new["id"]
    body = client.get(f"/api/projects/{pid}", headers=h(tok)).json()
    primary = body["layers"]["primary"]
    assert primary is not None
    assert primary["ext"] == "png"
    assert primary["url"].endswith("/{z}/{x}/{y}.png")
    assert primary["min_zoom"] == 0 and primary["max_zoom"] == 3
