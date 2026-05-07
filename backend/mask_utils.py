"""Encode/decode of 256x256 single-band PNG masks stored in SQLite."""
import io
import numpy as np
from PIL import Image

TILE_SIZE = 256
PIXELS = TILE_SIZE * TILE_SIZE  # 65536


def encode_mask(raw: bytes) -> bytes:
    """Convert raw 65536-byte array to single-band PNG bytes."""
    if len(raw) != PIXELS:
        raise ValueError(f"expected {PIXELS} bytes, got {len(raw)}")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(TILE_SIZE, TILE_SIZE)
    img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def decode_mask(png_bytes: bytes) -> bytes:
    """Convert PNG blob back to raw 65536-byte array."""
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    if img.size != (TILE_SIZE, TILE_SIZE):
        raise ValueError(f"invalid tile size {img.size}")
    arr = np.array(img, dtype=np.uint8)
    if arr.size != PIXELS:
        raise ValueError(f"decoded array size {arr.size}, expected {PIXELS}")
    return arr.tobytes()


def empty_mask_png() -> bytes:
    """PNG with all 65536 pixels set to 255 (unfilled)."""
    arr = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
    img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def validate_partial(raw: bytes, allowed_ids: list[int] | None = None) -> int:
    """Validate size and value range only (255 always allowed). Returns
    missing count (pixels with value 255).

    `allowed_ids` is the list of class ids the project accepts. When omitted,
    falls back to the legacy default-project lookup so callers that have
    not yet been migrated keep working."""
    if len(raw) != PIXELS:
        raise ValueError(f"expected {PIXELS} bytes, got {len(raw)}")
    arr = np.frombuffer(raw, dtype=np.uint8)
    if allowed_ids is None:
        allowed_ids = _default_project_class_ids()
    allowed = list(allowed_ids) + [255]
    valid = np.isin(arr, allowed)
    if not valid.all():
        raise ValueError("invalid class values present")
    return int((arr == 255).sum())


def validate_submission(
    raw: bytes,
    allowed_ids: list[int] | None = None,
    *,
    require_complete: bool = True,
) -> tuple[bool, int]:
    """Validate size and values. Returns (ok, missing_count).

    `require_complete=False` accepts any number of 255 pixels (project-level
    `mask_complete_required` flag); `True` rejects any."""
    missing = validate_partial(raw, allowed_ids=allowed_ids)
    if not require_complete:
        return True, missing
    return missing == 0, missing


def _default_project_class_ids() -> list[int]:
    """Look up the default project's class ids without requiring callers to
    pass them. Used as a fallback for legacy code paths still being migrated."""
    try:
        from .database import connect
        conn = connect()
        try:
            row = conn.execute(
                "SELECT id FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return []
            rows = conn.execute(
                "SELECT class_id FROM project_classes WHERE project_id=?",
                (row["id"],),
            ).fetchall()
            return [r["class_id"] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []
