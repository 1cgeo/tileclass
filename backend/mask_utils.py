"""Encode/decode of single-band PNG masks stored in SQLite. Tile pixel size
is per-project (projects.tile_px); the historical 256×256 lives on as
DEFAULT_TILE_SIZE for callers that haven't been threaded with a project."""
import io
import numpy as np
from PIL import Image

# Default geometry: matches the seed/legacy project. Modules importing
# TILE_SIZE/PIXELS for that purpose still get sensible values; per-tile work
# should pass tile_px explicitly via the helpers below.
DEFAULT_TILE_SIZE = 256
DEFAULT_PIXELS = DEFAULT_TILE_SIZE * DEFAULT_TILE_SIZE  # 65536

# Backward-compatible aliases. New code should not depend on these — they are
# constants only at the seed-default level.
TILE_SIZE = DEFAULT_TILE_SIZE
PIXELS = DEFAULT_PIXELS


def pixels_for(tile_px: int) -> int:
    """Body length expected for a tile of side `tile_px` pixels."""
    return tile_px * tile_px


def encode_mask(raw: bytes, tile_px: int = DEFAULT_TILE_SIZE) -> bytes:
    """Convert a raw `tile_px**2`-byte array to single-band PNG bytes."""
    expected = pixels_for(tile_px)
    if len(raw) != expected:
        raise ValueError(f"expected {expected} bytes, got {len(raw)}")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(tile_px, tile_px)
    img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def decode_mask(png_bytes: bytes, tile_px: int = DEFAULT_TILE_SIZE) -> bytes:
    """Convert PNG blob back to raw `tile_px**2`-byte array."""
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    if img.size != (tile_px, tile_px):
        raise ValueError(f"invalid tile size {img.size}, expected ({tile_px},{tile_px})")
    arr = np.array(img, dtype=np.uint8)
    expected = pixels_for(tile_px)
    if arr.size != expected:
        raise ValueError(f"decoded array size {arr.size}, expected {expected}")
    return arr.tobytes()


def empty_mask_png(tile_px: int = DEFAULT_TILE_SIZE) -> bytes:
    """PNG with all `tile_px**2` pixels set to 255 (unfilled)."""
    arr = np.full((tile_px, tile_px), 255, dtype=np.uint8)
    img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def validate_partial(raw: bytes, allowed_ids: list[int] | None = None,
                     *, tile_px: int = DEFAULT_TILE_SIZE) -> int:
    """Validate size and value range only (255 always allowed). Returns
    missing count (pixels with value 255).

    `allowed_ids` is the list of class ids the project accepts. When omitted,
    falls back to the legacy default-project lookup so callers that have
    not yet been migrated keep working."""
    expected = pixels_for(tile_px)
    if len(raw) != expected:
        raise ValueError(f"expected {expected} bytes, got {len(raw)}")
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
    tile_px: int = DEFAULT_TILE_SIZE,
) -> tuple[bool, int]:
    """Validate size and values. Returns (ok, missing_count).

    `require_complete=False` accepts any number of 255 pixels (project-level
    `mask_complete_required` flag); `True` rejects any."""
    missing = validate_partial(raw, allowed_ids=allowed_ids, tile_px=tile_px)
    if not require_complete:
        return True, missing
    return missing == 0, missing


def class_counts(raw: bytes) -> dict[int, int]:
    """Pixel count per class id present in `raw` (255/unfilled excluded).
    Returned as plain dict so callers can json-dump straight to the DB.
    Cheap C-loop count via numpy.bincount."""
    arr = np.frombuffer(raw, dtype=np.uint8)
    counts = np.bincount(arr, minlength=256)
    return {int(i): int(counts[i]) for i in range(255) if counts[i]}


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
