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
            "SELECT id, project_id, data_png, data_geojson, data_class_id, "
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
    project's kind: raster colorizes the painted mask, vector overlays
    LineStrings on the satellite backdrop."""
    row = _tile_row(tile_id)
    if not row:
        raise HTTPException(404, "tile not found")
    pid = row["project_id"]
    proj = project_service.get_project(pid) if pid else None
    kind = (proj or {}).get("kind")
    if kind == "vector":
        return _vector_thumbnail(row, proj, size)
    if kind == "classification":
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


def _vector_thumbnail(row, proj: dict, size: int) -> bytes:
    """Lines drawn on the satellite backdrop, sized down to `size`. When the
    backdrop isn't available, lines render on a dark background so the admin
    still sees the geometry."""
    import json as _json
    from PIL import ImageDraw
    from ..mask_tile_service import _vector_color_resolver
    pid = row["project_id"]
    try:
        backdrop = _tile_satellite_png(row["id"], size, row=row)
        from io import BytesIO
        base = Image.open(BytesIO(backdrop)).convert("RGBA")
    except Exception:
        # Satellite isn't available (no mbtiles, decompression-bomb guard
        # tripped on a degenerate bbox, etc). Lines need to render anyway.
        base = Image.new("RGBA", (size, size), (32, 32, 32, 255))

    text = row["data_geojson"]
    if not text:
        buf = io.BytesIO()
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    try:
        doc = _json.loads(text)
    except (TypeError, ValueError):
        doc = None
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if doc:
        draw = ImageDraw.Draw(overlay)
        color_for = _vector_color_resolver(proj)
        w, s, e, n = (row["bbox_west"], row["bbox_south"],
                      row["bbox_east"], row["bbox_north"])
        span_x = e - w
        span_y = n - s
        for f in doc.get("features", []) or []:
            geom = f.get("geometry") or {}
            if geom.get("type") != "LineString":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2 or span_x <= 0 or span_y <= 0:
                continue
            # Linear bbox→pixel — at thumbnail scale and tile size (~640m)
            # the spherical-Mercator skew is invisible.
            pts = [
                ((c[0] - w) / span_x * size,
                 (n - c[1]) / span_y * size)
                for c in coords if len(c) >= 2
            ]
            if len(pts) >= 2:
                draw.line(pts, fill=color_for(f.get("properties") or {}),
                          width=max(2, size // 64))
    composed = Image.alpha_composite(base, overlay)
    buf = io.BytesIO()
    composed.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _classification_thumbnail(row, proj: dict, size: int) -> bytes:
    """Satellite backdrop + colored border + class name centered. When the
    tile has not been classified yet, the satellite alone is returned so
    admins can still tell what's there."""
    from PIL import ImageDraw, ImageFont
    try:
        backdrop = _tile_satellite_png(row["id"], size, row=row)
        from io import BytesIO
        base = Image.open(BytesIO(backdrop)).convert("RGBA")
    except Exception:
        base = Image.new("RGBA", (size, size), (32, 32, 32, 255))

    cid = row["data_class_id"]
    if cid is None:
        buf = io.BytesIO()
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    cls = next((c for c in (proj.get("classes") or []) if c["id"] == cid), None)
    if cls is None:
        buf = io.BytesIO()
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    h = cls["color"].lstrip("#")
    rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Class-color border so the class is recognisable at a glance.
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
