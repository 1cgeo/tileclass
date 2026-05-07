"""Project CRUD + membership + class management."""
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
    # Primary URL is always present in default seed (config.yaml has tileserver.mbtiles_path).
    assert layers["primary"] == "/api/projects/1/xyz/primary/{z}/{x}/{y}"
    # Optional layers default to None when config has no path or path is empty.
    for k in ("secondary", "tertiary"):
        assert layers[k] is None or layers[k].endswith("/{z}/{x}/{y}")


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


def test_create_rejects_invalid_classes(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    r = client.post(
        "/api/admin/projects",
        json={
            "name": "broken",
            "primary_mbtiles": _stub_mbtiles(tmp_path),
            "classes": [
                {"id": 1, "name": "a", "color": "#aabbcc"},
                {"id": 1, "name": "dup", "color": "#aabbcc"},  # duplicate id
            ],
        },
        headers=h(tok),
    )
    assert r.status_code == 400


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
