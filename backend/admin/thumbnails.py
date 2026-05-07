"""Thumbnails for the admin grid: colorized mask preview + satellite backdrop.
Web Mercator math is local since these helpers only touch the backdrop pipeline
(the canonical bbox→pixel transform for masks lives in mask_tile_service)."""
import io
import math

from fastapi import HTTPException
import numpy as np
from PIL import Image

from .. import mbtiles_service, project_service
from ..database import connect
from ..mask_utils import decode_mask, TILE_SIZE, PIXELS


def _tile_row(tile_id: int):
    conn = connect()
    try:
        return conn.execute(
            "SELECT id, project_id, data_png, bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()


def _project_class_lut(project_id: int) -> np.ndarray:
    """Project-scoped RGBA lookup. Index = class id (0..255). Unfilled (255)
    and gaps stay [0,0,0,0] so they render transparent."""
    proj = project_service.get_project(project_id)
    classes = (proj or {}).get("classes") or []
    lut = np.zeros((256, 4), dtype=np.uint8)
    for c in classes:
        h = c["color"].lstrip("#")
        lut[c["id"]] = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    return lut


def tile_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Return the best thumbnail for the admin grid: colorized mask if the tile
    has any painted pixels, otherwise the satellite backdrop so empty tiles
    (pending / problem / freshly-assigned) still give the admin something to
    look at. Falls back to the transparent empty mask if the tile's project
    has no primary mbtiles open."""
    row = _tile_row(tile_id)
    if not row:
        raise HTTPException(404, "tile not found")
    pid = row["project_id"]
    primary = mbtiles_service.get_reader(pid, "primary") if pid else None
    # Missing or corrupt blob renders as the empty (all-255) mask — lut[255]
    # is transparent, so the admin grid shows a blank cell instead of a
    # broken image. Fail-open is the right call: a bad thumbnail is UI noise,
    # not a data-integrity signal the admin needs to act on.
    png = row["data_png"]
    if not png:
        raw = b"\xff" * PIXELS
    else:
        try:
            raw = decode_mask(png)
        except (ValueError, OSError):
            raw = b"\xff" * PIXELS
    if raw.count(b"\xff") == PIXELS and primary is not None and primary.is_open():
        try:
            return _tile_satellite_png(tile_id, size)
        except HTTPException:
            pass
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE)
    rgba = _project_class_lut(pid)[arr]
    img = Image.fromarray(rgba, mode="RGBA")
    if size != TILE_SIZE:
        img = img.resize((size, size), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _lon_to_tile_x(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << z)


def _lat_to_tile_y(lat: float, z: int) -> float:
    # Web Mercator: clamp to valid range to avoid math domain errors near poles.
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    return (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (1 << z)


def _tile_satellite_png(tile_id: int, size: int = 128) -> bytes:
    """Composite satellite imagery from the tile's project primary mbtiles.
    Raises 404 if the project's primary isn't open or the tile isn't found —
    callers decide whether to fall through."""
    row = _tile_row(tile_id)
    if not row:
        raise HTTPException(404, "tile not found")
    pid = row["project_id"]
    primary = mbtiles_service.get_reader(pid, "primary") if pid else None
    if primary is None or not primary.is_open():
        raise HTTPException(404, "satellite source not available")
    w, s, e, n = row["bbox_west"], row["bbox_south"], row["bbox_east"], row["bbox_north"]
    min_z, max_z = primary.zoom_range()
    zoom = max_z if max_z is not None else 19

    x0_f = _lon_to_tile_x(w, zoom)
    x1_f = _lon_to_tile_x(e, zoom)
    # Web Mercator y grows southward: north lat → smaller y.
    y0_f = _lat_to_tile_y(n, zoom)
    y1_f = _lat_to_tile_y(s, zoom)
    x0, x1 = math.floor(x0_f), math.ceil(x1_f)
    y0, y1 = math.floor(y0_f), math.ceil(y1_f)

    TS = 256
    canvas = Image.new("RGB", ((x1 - x0) * TS, (y1 - y0) * TS), (32, 32, 32))
    for tx in range(x0, x1):
        for ty in range(y0, y1):
            data = primary.get_tile(zoom, tx, ty)
            if not data:
                continue
            try:
                src = Image.open(io.BytesIO(data)).convert("RGB")
            except (OSError, ValueError):
                continue
            canvas.paste(src, ((tx - x0) * TS, (ty - y0) * TS))

    crop_box = (
        (x0_f - x0) * TS,
        (y0_f - y0) * TS,
        (x1_f - x0) * TS,
        (y1_f - y0) * TS,
    )
    cropped = canvas.crop(crop_box)
    if cropped.size != (size, size):
        cropped = cropped.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def tile_satellite_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Public endpoint wrapper: always serve satellite, even when the mask is
    non-empty. Used by callers that explicitly want the backdrop regardless of
    mask state."""
    return _tile_satellite_png(tile_id, size)
