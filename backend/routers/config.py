"""Read-only config endpoints + raw MBTiles passthroughs (sat/wc/mb)."""
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
    wc = mbtiles_service.worldcover
    mb = mbtiles_service.mapbiomas
    min_zoom = max_zoom = None
    if primary.is_open():
        url = f"/api/xyz/{{z}}/{{x}}/{{y}}.{primary.tile_format()}"
        min_zoom, max_zoom = primary.zoom_range()
    else:
        url = ts.get("url_template", "")
    wc_url = None
    wc_min = wc_max = None
    if wc.is_open():
        wc_url = f"/api/wc/{{z}}/{{x}}/{{y}}.{wc.tile_format()}"
        wc_min, wc_max = wc.zoom_range()
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
        "wc_url_template": wc_url,
        "wc_min_zoom": wc_min,
        "wc_max_zoom": wc_max,
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


@router.get("/api/wc/{z}/{x}/{y}.{ext}")
def wc_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_mbtiles(mbtiles_service.worldcover, z, x, y, ext)


@router.get("/api/mb/{z}/{x}/{y}.{ext}")
def mb_mbtiles_xyz(z: int, x: int, y: int, ext: str):
    return _serve_mbtiles(mbtiles_service.mapbiomas, z, x, y, ext)
