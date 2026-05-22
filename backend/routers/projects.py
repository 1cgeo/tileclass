"""Project endpoints — list/details (any member) + admin CRUD + raw XYZ
passthroughs. The XYZ endpoint pulls bytes from the per-project reader pool
in mbtiles_service so layers stay scoped to a single project."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from .. import auth, export_service, mbtiles_service, project_service
from ..models import (
    ProjectCreateIn, ProjectUpdateIn, ProjectClassesIn, ProjectAttributesIn,
    ProjectMemberIn,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
admin_router = APIRouter(prefix="/api/admin/projects", tags=["admin", "projects"])


# ---- Read (any authenticated user) -----------------------------------------

@router.get("")
def list_projects(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return project_service.list_projects_for_user(user.id, is_admin=user.role == "admin")


_REMOTE_DEFAULT_EXT = {"primary": "webp", "secondary": "webp", "tertiary": "webp",
                       "ref_primary": "png", "ref_secondary": "png"}


def _ext_from_url(url: str) -> str | None:
    # `https://host/.../{z}/{x}/{y}.png?key=…` → png
    head, _, _ = url.partition("?")
    if "." in head:
        ext = head.rsplit(".", 1)[-1].lower()
        if ext in ("png", "webp", "jpg", "jpeg"):
            return ext
    return None


def _build_layers(project_id: int, proj: dict) -> dict:
    """Layer URL templates the editor consumes. Local mbtiles get the
    proxied `/api/projects/{id}/xyz/...` URL; remote tile-server URLs
    (Martin/TileServer-GL) pass through directly so MapLibre fetches them
    without a backend hop. Absent layer → None; configured-but-broken
    mbtiles → {error: 'mbtiles_not_open'} so admins can spot it."""
    base = f"/api/projects/{project_id}/xyz"
    out = {}
    for layer in project_service.LAYER_KEYS:
        src = project_service.layer_path(proj, layer)
        if not src:
            out[layer] = None
            continue
        if project_service.is_remote_layer(src):
            ext = _ext_from_url(src) or _REMOTE_DEFAULT_EXT[layer]
            out[layer] = {
                "url": src,
                "ext": ext,
                "min_zoom": 0,
                "max_zoom": 22,
                "remote": True,
            }
            continue
        reader = mbtiles_service.get_reader(project_id, layer)
        if reader is None:
            out[layer] = {"url": None, "ext": None, "min_zoom": None,
                          "max_zoom": None, "error": "mbtiles_not_open"}
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
    return {
        **proj, "role": role,
        "layers": _build_layers(project_id, proj),
        # Surfaces to the admin form so the tile_px/meters_per_pixel inputs
        # disable once any tile exists (mask bytes / bbox would mismatch).
        "tile_geometry_locked": project_service.has_any_tile(project_id),
    }


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
        kind=body.kind,
        topology_required=body.topology_required,
        box_required=body.box_required,
        mask_complete_required=body.mask_complete_required,
        tile_px=body.tile_px,
        meters_per_pixel=body.meters_per_pixel,
        primary_mbtiles=body.primary_mbtiles,
        secondary_mbtiles=body.secondary_mbtiles,
        tertiary_mbtiles=body.tertiary_mbtiles,
        ref_mask_primary_mbtiles=body.ref_mask_primary_mbtiles,
        ref_mask_secondary_mbtiles=body.ref_mask_secondary_mbtiles,
        classes=[c.model_dump() for c in body.classes] if body.classes else None,
        attributes=[a.model_dump() for a in body.attributes] if body.attributes else None,
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


@admin_router.get("/{project_id}/export")
def export_project(
    project_id: int,
    status: str = Query("reviewed", pattern="^(reviewed|classified|reviewed_classified)$"),
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    """Stream the project's finished data as a ZIP. Format follows the project
    kind (raster→GeoTIFF, vector/detection→GeoJSON, classification→CSV); a
    manifest is always included. `status` selects reviewed-only or
    reviewed+classified. The X-Tile-Count header reports how many tiles went in."""
    try:
        data, fname, count = export_service.export_zip(project_id, status)
    except ValueError:
        raise HTTPException(400, detail={"error": "invalid_status"})
    except LookupError:
        raise HTTPException(404, detail={"error": "project_not_found"})
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Tile-Count": str(count),
            # Let the browser fetch read the count header cross-fetch.
            "Access-Control-Expose-Headers": "X-Tile-Count, Content-Disposition",
        },
    )


class ProjectCloneIn(BaseModel):
    name: str | None = None


@admin_router.post("/{project_id}/clone")
def clone_project(
    project_id: int,
    body: ProjectCloneIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    """Duplicate the project's config (paths, classes, mask flag) into a
    new project. Memberships are not copied; admin assigns explicitly."""
    return project_service.clone_project(
        project_id, new_name=body.name, by_user=admin.id,
    )


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


@admin_router.put("/{project_id}/attributes")
def replace_attributes(
    project_id: int,
    body: ProjectAttributesIn,
    admin: auth.CurrentUser = Depends(auth.require_admin),
):
    """Vector projects only — replace the per-feature attribute schema.
    Removing a key in use by any feature → 409 attribute_in_use."""
    return project_service.set_attributes(
        project_id,
        [a.model_dump() for a in body.attributes],
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
