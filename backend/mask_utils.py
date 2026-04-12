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
        raise ValueError(f"decoded array size {arr.size}")
    return arr.tobytes()


def empty_mask_png() -> bytes:
    """PNG with all 65536 pixels set to 255 (unfilled)."""
    arr = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
    img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def validate_submission(raw: bytes) -> tuple[bool, int]:
    """Validate size and values. Returns (ok, missing_count). Missing = pixels with value 255."""
    if len(raw) != PIXELS:
        raise ValueError(f"expected {PIXELS} bytes, got {len(raw)}")
    arr = np.frombuffer(raw, dtype=np.uint8)
    valid = np.isin(arr, [1, 2, 3, 4, 5, 6, 255])
    if not valid.all():
        raise ValueError("invalid class values present")
    missing = int((arr == 255).sum())
    return missing == 0, missing
