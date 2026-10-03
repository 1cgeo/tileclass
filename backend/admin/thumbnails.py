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
from ..mask_utils import decode_mask


def _tile_row(tile_id: int):
    conn = connect()
    try:
        return conn.execute(
            "SELECT id, project_id, data_png, data_class_id, "
            "bbox_west, bbox_south, bbox_east, bbox_north "
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
    """Return the best thumbnail for the admin grid. Dispatches by the
    project's kind: raster colorizes the painted mask; classification badges
    the tile with its class name."""
    row = _tile_row(tile_id)
    if not row:
        raise HTTPException(404, "tile not found")
    pid = row["project_id"]
    proj = project_service.get_project(pid) if pid else None
    if (proj or {}).get("kind") == "classification":
        return _classification_thumbnail(row, proj, size)
    primary = mbtiles_service.get_reader(pid, "primary") if pid else None
    tile_px = int((proj or {}).get("tile_px", 256))
    pixels = tile_px * tile_px
    # Missing or corrupt blob renders as the empty (all-255) mask — lut[255]
    # is transparent, so the admin grid shows a blank cell instead of a
    # broken image. Fail-open is the right call: a bad thumbnail is UI noise,
    # not a data-integrity signal the admin needs to act on.
    png = row["data_png"]
    if not png:
        raw = b"\xff" * pixels
    else:
        try:
            raw = decode_mask(png, tile_px)
        except (ValueError, OSError):
            raw = b"\xff" * pixels
    if raw.count(b"\xff") == pixels and primary is not None and primary.is_open():
        try:
            return _tile_satellite_png(tile_id, size, row=row)
        except HTTPException:
            pass
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(tile_px, tile_px)
    rgba = _project_class_lut(pid)[arr]
    img = Image.fromarray(rgba, mode="RGBA")
    if size != tile_px:
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


def _tile_satellite_png(tile_id: int, size: int = 128, *, row=None) -> bytes:
    """Composite satellite imagery from the tile's project primary mbtiles.
    Raises 404 if the project's primary isn't open or the tile isn't found —
    callers decide whether to fall through. Pass `row` to skip the SELECT
    when the caller already fetched it."""
    if row is None:
        row = _tile_row(tile_id)
    if not row:
        raise HTTPException(404, "tile not found")
    pid = row["project_id"]
    primary = mbtiles_service.get_reader(pid, "primary") if pid else None
    if primary is None or not primary.is_open():
        raise HTTPException(404, "satellite source not available")
    bbox = (row["bbox_west"], row["bbox_south"], row["bbox_east"], row["bbox_north"])
    min_z, max_z = primary.zoom_range()
    lo = 0 if min_z is None else int(min_z)
    hi = _DEFAULT_MAX_Z if max_z is None else int(max_z)
    zoom = _pick_zoom(bbox, size, lo, hi)
    cropped = _compose(primary, bbox, zoom)
    # Sparse pyramids (only the top zoom populated, e.g. a partial
    # build_mbtiles run) yield nothing at the chosen zoom: retry once at the
    # source max zoom, unless that would read more than _MAX_SOURCE_READS.
    if cropped is None and zoom < hi and _source_tile_count(bbox, hi) <= _MAX_SOURCE_READS:
        cropped = _compose(primary, bbox, hi)
    if cropped is None:
        cropped = Image.new("RGB", (size, size), (32, 32, 32))
    if cropped.size != (size, size):
        cropped = cropped.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


_TS = 256  # source XYZ tile edge in pixels
_DEFAULT_MAX_Z = 19
_MAX_SOURCE_READS = 144  # e.g. a 640 m tile at z19


def _span_px(bbox, z: int) -> float:
    """Pixels across the bbox's narrower side when composed at zoom z."""
    w, s, e, n = bbox
    width = (_lon_to_tile_x(e, z) - _lon_to_tile_x(w, z)) * _TS
    height = (_lat_to_tile_y(s, z) - _lat_to_tile_y(n, z)) * _TS
    return min(width, height)


def _pick_zoom(bbox, size: int, lo: int, hi: int) -> int:
    """Lowest zoom in [lo, hi] whose resolution still gives >= `size` pixels
    across the tile. Composing at max zoom instead reads ~100 source tiles for
    a 640 m tile at z19 (and explodes memory for large tile_px)."""
    if hi < lo:
        lo = hi
    base = _span_px(bbox, 0)
    if base <= 0:
        return hi
    z = math.ceil(math.log2(size / base)) if size > base else 0
    z = max(lo, min(hi, z))
    # Guard float rounding around the power-of-two boundary.
    while z < hi and _span_px(bbox, z) < size:
        z += 1
    while z > lo and _span_px(bbox, z - 1) >= size:
        z -= 1
    return z


def _tile_range(bbox, z: int) -> tuple[float, float, float, float, int, int, int, int]:
    w, s, e, n = bbox
    x0_f, x1_f = _lon_to_tile_x(w, z), _lon_to_tile_x(e, z)
    # Web Mercator y grows southward: north lat → smaller y.
    y0_f, y1_f = _lat_to_tile_y(n, z), _lat_to_tile_y(s, z)
    x0, x1 = math.floor(x0_f), max(math.ceil(x1_f), math.floor(x0_f) + 1)
    y0, y1 = math.floor(y0_f), max(math.ceil(y1_f), math.floor(y0_f) + 1)
    return x0_f, y0_f, x1_f, y1_f, x0, y0, x1, y1


def _source_tile_count(bbox, z: int) -> int:
    *_, x0, y0, x1, y1 = _tile_range(bbox, z)
    return (x1 - x0) * (y1 - y0)


def _compose(primary, bbox, zoom: int):
    """Mosaic the source tiles covering bbox at `zoom` and crop to the bbox.
    Returns None when no source tile had data at this zoom."""
    x0_f, y0_f, x1_f, y1_f, x0, y0, x1, y1 = _tile_range(bbox, zoom)
    canvas = Image.new("RGB", ((x1 - x0) * _TS, (y1 - y0) * _TS), (32, 32, 32))
    found = False
    for tx in range(x0, x1):
        for ty in range(y0, y1):
            data = primary.get_tile(zoom, tx, ty)
            if not data:
                continue
            try:
                src = Image.open(io.BytesIO(data)).convert("RGB")
            except (OSError, ValueError):
                continue
            if src.size != (_TS, _TS):
                src = src.resize((_TS, _TS), Image.LANCZOS)
            canvas.paste(src, ((tx - x0) * _TS, (ty - y0) * _TS))
            found = True
    if not found:
        return None
    return canvas.crop((
        (x0_f - x0) * _TS,
        (y0_f - y0) * _TS,
        (x1_f - x0) * _TS,
        (y1_f - y0) * _TS,
    ))


def tile_satellite_thumbnail(tile_id: int, size: int = 128) -> bytes:
    """Public endpoint wrapper: always serve satellite, even when the mask is
    non-empty. Used by callers that explicitly want the backdrop regardless of
    mask state."""
    return _tile_satellite_png(tile_id, size)


def _classification_thumbnail(row, proj: dict, size: int) -> bytes:
    """Satellite backdrop + colored border + class name centered. When the
    tile has not been classified yet, just the satellite is returned so
    admins can still tell what's there."""
    from PIL import ImageDraw, ImageFont
    from io import BytesIO
    from ..mask_tile_service import _hex_to_rgb
    try:
        base = Image.open(BytesIO(_tile_satellite_png(row["id"], size, row=row))).convert("RGBA")
    except Exception:
        base = Image.new("RGBA", (size, size), (32, 32, 32, 255))

    cid = row["data_class_id"]
    cls = next((c for c in (proj.get("classes") or []) if c["id"] == cid), None) if cid is not None else None
    if cls is None:
        buf = io.BytesIO()
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    rgb = _hex_to_rgb(cls["color"])
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    border = max(2, size // 32)
    draw.rectangle([0, 0, size - 1, size - 1],
                   outline=(*rgb, 240), width=border)
    label = cls["name"]
    try:
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except (OSError, AttributeError):
        font = None
        tw, th = (len(label) * 6, 11)
    pad = max(3, size // 40)
    box = [size // 2 - tw // 2 - pad, size - th - 2 * pad,
           size // 2 + tw // 2 + pad, size - pad]
    draw.rectangle(box, fill=(*rgb, 200))
    draw.text((size // 2 - tw // 2, size - th - pad - 1), label,
              fill=(255, 255, 255, 255), font=font)
    composed = Image.alpha_composite(base, overlay)
    buf = io.BytesIO()
    composed.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
