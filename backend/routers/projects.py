"""Project endpoints — list/details (any member) + admin CRUD + raw XYZ
passthroughs. The XYZ endpoint pulls bytes from the per-project reader pool
in mbtiles_service so layers stay scoped to a single project."""
from fastapi import APIRouter, Depends, HTTPException, Response

from .. import auth, mbtiles_service, project_service
from ..models import (
    ProjectCreateIn, ProjectUpdateIn, ProjectClassesIn, ProjectMemberIn,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
admin_router = APIRouter(prefix="/api/admin/projects", tags=["admin", "projects"])


# ---- Read (any authenticated user) -----------------------------------------

@router.get("")
def list_projects(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return project_service.list_projects_for_user(user.id, is_admin=user.role == "admin")


def _build_layers(project_id: int, proj: dict) -> dict:
    """Layer URL templates the editor consumes. Each entry includes the
    extension (taken from the mbtiles metadata, falling back to a per-layer
    default) so the editor can construct the final URL without round-tripping
    to discover the format. Absent layers map to None — the client uses that
    to suppress shortcuts/legend entries."""
    base = f"/api/projects/{project_id}/xyz"
    out = {}
    layer_to_field = {
        "primary": "primary_mbtiles",
        "secondary": "secondary_mbtiles",
        "tertiary": "tertiary_mbtiles",
        "ref_primary": "ref_mask_primary_mbtiles",
        "ref_secondary": "ref_mask_secondary_mbtiles",
    }
    for layer, field in layer_to_field.items():
        if not proj.get(field):
            out[layer] = None
            continue
        reader = mbtiles_service.get_reader(project_id, layer)
        if reader is None:
            # Path is set but file missing/unreadable. Surface that the layer
            # is configured so the admin can spot the problem; client treats
            # it as absent (no shortcut).
            out[layer] = {
                "url": None,
                "ext": None,
                "min_zoom": None,
                "max_zoom": None,
                "error": "mbtiles_not_open",
            }
            continue
        ext = reader.tile_format()
        zmin, zmax = reader.zoom_range()
        out[layer] = {
            "url": f"{base}/{layer}/{{z}}/{{x}}/{{y}}.{ext}",
            "ext": ext,
            "min_zoom": zmin,
            "max_zoom": zmax,
        }
    return out


@router.get("/{project_id}")
def get_project(project_id: int, user: auth.CurrentUser = Depends(auth.get_current_user)):
    proj = project_service.get_project(project_id)
    if not proj:
        raise HTTPException(404, detail={"error": "project_not_found"})
    role = project_service.require_membership(project_id, user)
    return {**proj, "role": role, "layers": _build_layers(project_id, proj)}


# ---- XYZ passthrough --------------------------------------------------------

_LAYER_DEFAULT_MEDIA = {
    "webp": "image/webp",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


@router.get("/{project_id}/xyz/{layer}/{z}/{x}/{y}.{ext}")
def project_xyz(
    project_id: int, layer: str, z: int, x: int, y: int, ext: str,
    user: auth.CurrentUser = Depends(auth.get_current_user),
):
    project_service.require_membership(project_id, user)
    reader = mbtiles_service.get_reader(project_id, layer)
    if reader is None:
        raise HTTPException(404, detail="layer not configured")
    if ext.lower() != reader.tile_format():
        raise HTTPException(404, detail="wrong extension")
    data = reader.get_tile(z, x, y)
    if data is None:
        return Response(status_code=204)
    media = _LAYER_DEFAULT_MEDIA.get(ext.lower(), f"image/{ext.lower()}")
    return Response(
        content=data,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# ---- Admin write ------------------------------------------------------------

@admin_router.post("")
def create_project(
    body: ProjectCreateIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    return project_service.create_project(
        name=body.name,
        description=body.description,
        mask_complete_required=body.mask_complete_required,
        primary_mbtiles=body.primary_mbtiles,
        secondary_mbtiles=body.secondary_mbtiles,
        tertiary_mbtiles=body.tertiary_mbtiles,
        ref_mask_primary_mbtiles=body.ref_mask_primary_mbtiles,
        ref_mask_secondary_mbtiles=body.ref_mask_secondary_mbtiles,
        classes=[c.model_dump() for c in body.classes],
        created_by=admin.id,
    )


@admin_router.patch("/{project_id}")
def update_project(
    project_id: int,
    body: ProjectUpdateIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    fields = body.model_dump(exclude_unset=True)
    return project_service.update_project(project_id, fields=fields, updated_by=admin.id)


@admin_router.delete("/{project_id}")
def delete_project(
    project_id: int,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    project_service.delete_project(project_id, by_user=admin.id)
    return {"ok": True}


@admin_router.put("/{project_id}/classes")
def replace_classes(
    project_id: int,
    body: ProjectClassesIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    return project_service.set_classes(
        project_id,
        [c.model_dump() for c in body.classes],
        updated_by=admin.id,
    )


@admin_router.get("/{project_id}/members")
def list_members(
    project_id: int,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    return project_service.list_members(project_id)


@admin_router.post("/{project_id}/members")
def upsert_member(
    project_id: int,
    body: ProjectMemberIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    project_service.add_member(project_id, body.user_id, body.role, by_user=admin.id)
    return {"ok": True}


@admin_router.delete("/{project_id}/members/{user_id}")
def delete_member(
    project_id: int,
    user_id: int,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    project_service.remove_member(project_id, user_id, by_user=admin.id)
    return {"ok": True}
