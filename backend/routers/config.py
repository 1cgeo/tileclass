"""Read-only config endpoints + raw MBTiles passthroughs (sat/dsg/mb)."""
from fastapi import APIRouter, HTTPException, Path, Response

from .. import mbtiles_service
from ..config import get_config
from ..models import ClassOut

router = APIRouter(tags=["config"])


@router.get("/api/config/classes", response_model=list[ClassOut])
def config_classes():
    return get_config()["classes"]


@router.get("/api/config/tileserver")
def config_tileserver():
    cfg = get_config()
    ts = cfg.get("tileserver") or {}
    ts2 = cfg.get("tileserver_secondary") or {}
    ts3 = cfg.get("tileserver_tertiary") or {}
    primary = mbtiles_service.primary
    dsg = mbtiles_service.dsg
    mb = mbtiles_service.mapbiomas
    min_zoom = max_zoom = None
    if primary.is_open():
        url = f"/api/xyz/{{z}}/{{x}}/{{y}}.{primary.tile_format()}"
        min_zoom, max_zoom = primary.zoom_range()
    else:
        url = ts.get("url_template", "")
    dsg_url = None
    dsg_min = dsg_max = None
    if dsg.is_open():
        dsg_url = f"/api/dsg/{{z}}/{{x}}/{{y}}.{dsg.tile_format()}"
        dsg_min, dsg_max = dsg.zoom_range()
    mb_url = None
    mb_min = mb_max = None
    if mb.is_open():
        mb_url = f"/api/mb/{{z}}/{{x}}/{{y}}.{mb.tile_format()}"
        mb_min, mb_max = mb.zoom_range()
    return {
        "url_template": url,
        "secondary_url_template": ts2.get("url_template"),
        "tertiary_url_template": ts3.get("url_template"),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "secondary_max_zoom": ts2.get("max_zoom", 22),
        "tertiary_max_zoom": ts3.get("max_zoom", 22),
        "dsg_url_template": dsg_url,
        "dsg_min_zoom": dsg_min,
        "dsg_max_zoom": dsg_max,
        "mb_url_template": mb_url,
        "mb_min_zoom": mb_min,
        "mb_max_zoom": mb_max,
    }


def _serve_mbtiles(reader: mbtiles_service.MBTilesReader, z: int, x: int, y: int, ext: str):
    if not reader.is_open():
        raise HTTPException(status_code=404, detail="mbtiles not configured")
    if ext.lower() != reader.tile_format():
        raise HTTPException(status_code=404, detail="wrong extension")
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
    return _serve_mbtiles(mbtiles_service.primary, z, x, y, ext)


@router.get("/api/dsg/{z}/{x}/{y}.{ext}")
def dsg_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_mbtiles(mbtiles_service.dsg, z, x, y, ext)


@router.get("/api/mb/{z}/{x}/{y}.{ext}")
def mb_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_mbtiles(mbtiles_service.mapbiomas, z, x, y, ext)
