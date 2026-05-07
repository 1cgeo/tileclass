"""Project endpoints — list/details (any member) + admin CRUD.

Layer-byte serving (`/api/projects/{id}/xyz/{layer}/...`) lives next to the
mbtiles cache logic in routers/config.py to share the per-project reader pool.
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import auth, project_service
from ..models import (
    ProjectCreateIn, ProjectUpdateIn, ProjectClassesIn, ProjectMemberIn,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
admin_router = APIRouter(prefix="/api/admin/projects", tags=["admin", "projects"])


# ---- Read (any authenticated user) -----------------------------------------

@router.get("")
def list_projects(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return project_service.list_projects_for_user(user.id, is_admin=user.role == "admin")


@router.get("/{project_id}")
def get_project(project_id: int, user: auth.CurrentUser = Depends(auth.get_current_user)):
    proj = project_service.get_project(project_id)
    if not proj:
        raise HTTPException(404, detail={"error": "project_not_found"})
    role = project_service.require_membership(project_id, user)
    out = {**proj, "role": role}
    # Layer URLs the editor will hit. Absent layers map to None so the client
    # knows not to register the corresponding shortcut.
    base = f"/api/projects/{project_id}/xyz"
    out["layers"] = {
        "primary": f"{base}/primary/{{z}}/{{x}}/{{y}}" if proj["primary_mbtiles"] else None,
        "secondary": f"{base}/secondary/{{z}}/{{x}}/{{y}}" if proj["secondary_mbtiles"] else None,
        "tertiary": f"{base}/tertiary/{{z}}/{{x}}/{{y}}" if proj["tertiary_mbtiles"] else None,
        "ref_primary": f"{base}/ref_primary/{{z}}/{{x}}/{{y}}" if proj["ref_mask_primary_mbtiles"] else None,
        "ref_secondary": f"{base}/ref_secondary/{{z}}/{{x}}/{{y}}" if proj["ref_mask_secondary_mbtiles"] else None,
    }
    return out


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
