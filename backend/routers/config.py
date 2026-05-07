"""Legacy read-only config endpoints. Kept as thin aliases over the
default project (project_id=1) so older clients keep working until the SPA
fully migrates to /api/projects/{id} (planned removal in step 9)."""
from fastapi import APIRouter, HTTPException, Response

from .. import mbtiles_service, project_service
from ..models import ClassOut

router = APIRouter(tags=["config"])


def _default_project() -> dict | None:
    """Returns the seed default project (lowest id) for legacy aliasing."""
    from ..database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE active=1 ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return project_service.get_project(row["id"]) if row else None


@router.get("/api/config/classes", response_model=list[ClassOut])
def config_classes():
    proj = _default_project()
    return proj["classes"] if proj else []


@router.get("/api/config/tileserver")
def config_tileserver():
    """Backwards-compatible shape derived from the default project."""
    proj = _default_project()
    if not proj:
        return {}
    pid = proj["id"]
    base = f"/api/projects/{pid}/xyz"

    def _info(layer: str):
        reader = mbtiles_service.get_reader(pid, layer)
        if reader is None:
            return None, None, None, None
        ext = reader.tile_format()
        zmin, zmax = reader.zoom_range()
        return f"{base}/{layer}/{{z}}/{{x}}/{{y}}.{ext}", ext, zmin, zmax

    primary_url, _, p_min, p_max = _info("primary")
    sec_url, _, _, s_max = _info("secondary")
    ter_url, _, _, t_max = _info("tertiary")
    dsg_url, _, dsg_min, dsg_max = _info("ref_primary")
    mb_url, _, mb_min, mb_max = _info("ref_secondary")
    return {
        "url_template": primary_url or "",
        "secondary_url_template": sec_url,
        "tertiary_url_template": ter_url,
        "min_zoom": p_min,
        "max_zoom": p_max,
        "secondary_max_zoom": s_max if s_max is not None else 22,
        "tertiary_max_zoom": t_max if t_max is not None else 22,
        "dsg_url_template": dsg_url,
        "dsg_min_zoom": dsg_min,
        "dsg_max_zoom": dsg_max,
        "mb_url_template": mb_url,
        "mb_min_zoom": mb_min,
        "mb_max_zoom": mb_max,
    }


_LEGACY_LAYERS = {
    "xyz": "primary",
    "dsg": "ref_primary",
    "mb": "ref_secondary",
}


def _serve_default_layer(legacy_key: str, z: int, x: int, y: int, ext: str):
    proj = _default_project()
    if not proj:
        raise HTTPException(404, detail="no project configured")
    layer = _LEGACY_LAYERS[legacy_key]
    reader = mbtiles_service.get_reader(proj["id"], layer)
    if reader is None:
        raise HTTPException(404, detail="mbtiles not configured")
    if ext.lower() != reader.tile_format():
        raise HTTPException(404, detail="wrong extension")
    data = reader.get_tile(z, x, y)
    if data is None:
        return Response(status_code=204)
    media = "image/webp" if ext.lower() == "webp" else f"image/{ext.lower()}"
    return Response(
        content=data,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/api/xyz/{z}/{x}/{y}.{ext}")
def mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_default_layer("xyz", z, x, y, ext)


@router.get("/api/dsg/{z}/{x}/{y}.{ext}")
def dsg_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_default_layer("dsg", z, x, y, ext)


@router.get("/api/mb/{z}/{x}/{y}.{ext}")
def mb_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_default_layer("mb", z, x, y, ext)
