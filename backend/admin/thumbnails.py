"""Thumbnails for the admin grid: colorized mask preview + satellite backdrop.
Web Mercator math is local since these helpers only touch the backdrop pipeline
(the canonical bbox→pixel transform for masks lives in mask_tile_service)."""
import io
import math

from fastapi import HTTPException
import numpy as np
from PIL import Image

from .. import mbtiles_service
from ..config import get_config
from ..database import connect
from ..mask_utils import decode_mask, TILE_SIZE, PIXELS


def tile_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Return the best thumbnail for the admin grid: colorized mask if the tile
    has any painted pixels, otherwise the satellite backdrop so empty tiles
    (pending / problem / freshly-assigned) still give the admin something to
    look at. Falls back to the transparent empty mask if MBTiles isn't open."""
    conn = connect()
    try:
        row = conn.execute("SELECT data_png FROM tiles WHERE id=?", (tile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
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
    # Empty mask (no painted pixels) → prefer satellite backdrop when available.
    # `count(b"\xff")` is a C-level byte scan on the bytes object, cheaper than
    # any numpy detour — we only fall through to the colorized render when
    # there's at least one non-255 pixel, or MBTiles isn't open.
    if raw.count(b"\xff") == PIXELS and mbtiles_service.primary.is_open():
        try:
            return _tile_satellite_png(tile_id, size)
        except HTTPException:
            pass
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE)
    classes = get_config()["classes"]
    lut = np.zeros((256, 4), dtype=np.uint8)
    for c in classes:
        h = c["color"].lstrip("#")
        lut[c["id"]] = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    rgba = lut[arr]
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
    """Composite satellite imagery from MBTiles over the tile bbox and return a PNG.
    Shared by the empty-mask fallback in `tile_thumbnail` and the dedicated
    `/satellite-thumbnail` endpoint. Raises 404 if MBTiles isn't open or the
    tile isn't found — callers decide whether to fall through."""
    if not mbtiles_service.primary.is_open():
        raise HTTPException(404, "satellite source not available")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT bbox_west, bbox_south, bbox_east, bbox_north FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "tile not found")
    w, s, e, n = row["bbox_west"], row["bbox_south"], row["bbox_east"], row["bbox_north"]
    min_z, max_z = mbtiles_service.primary.zoom_range()
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
            data = mbtiles_service.primary.get_tile(zoom, tx, ty)
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
