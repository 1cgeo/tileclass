"""Unit tests for mask_utils — PNG round-trip and validation."""
import io
import numpy as np
import pytest
from PIL import Image
from backend.mask_utils import (
    encode_mask, decode_mask, empty_mask_png, validate_submission,
    PIXELS, TILE_SIZE,
)


def test_encode_decode_roundtrip_preserves_bytes():
    """Roundtrip must cover full byte range {1..6, 255} AND be position-preserving."""
    rng = np.random.default_rng(42)
    arr = rng.choice([1, 2, 3, 4, 5, 6, 255], size=PIXELS).astype(np.uint8)
    # Plant sentinels at known positions to detect row/col transposition
    arr[0] = 255                 # top-left
    arr[TILE_SIZE - 1] = 1       # top-right
    arr[TILE_SIZE * (TILE_SIZE - 1)] = 2  # bottom-left
    arr[PIXELS - 1] = 6          # bottom-right
    arr[TILE_SIZE + 3] = 4       # row 1, col 3
    raw = arr.tobytes()

    png = encode_mask(raw)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    decoded = decode_mask(png)
    assert decoded == raw  # full-byte equality across all 65536 positions
    # Explicit positional checks (guard against transpose bugs that random eq might mask)
    d = np.frombuffer(decoded, dtype=np.uint8)
    assert d[0] == 255
    assert d[TILE_SIZE - 1] == 1
    assert d[TILE_SIZE * (TILE_SIZE - 1)] == 2
    assert d[PIXELS - 1] == 6
    assert d[TILE_SIZE + 3] == 4


def test_encode_produces_256x256_grayscale_png():
    """encode_mask must output a real 256×256 single-band PNG, not just any PNG."""
    raw = np.full(PIXELS, 3, dtype=np.uint8).tobytes()
    png = encode_mask(raw)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size == (TILE_SIZE, TILE_SIZE)
    assert img.mode == "L"


def test_empty_mask_all_pixels_are_255():
    """Every one of the 65536 bytes must be 255, not just edges."""
    raw = decode_mask(empty_mask_png())
    assert len(raw) == PIXELS
    arr = np.frombuffer(raw, dtype=np.uint8)
    assert int((arr == 255).sum()) == PIXELS
    # And validate_submission agrees: all missing
    ok, missing = validate_submission(raw)
    assert ok is False and missing == PIXELS


def test_decode_rejects_wrong_size_png():
    """A PNG of non-256×256 dims must raise, not silently mangle."""
    img = Image.new("L", (128, 128), 1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with pytest.raises(ValueError):
        decode_mask(buf.getvalue())


@pytest.mark.parametrize("bad_size", [0, 1, 100, PIXELS - 1, PIXELS + 1, PIXELS * 2])
def test_encode_wrong_size_raises(bad_size):
    with pytest.raises(ValueError):
        encode_mask(b"\x01" * bad_size)


def test_validate_accepts_fully_filled():
    raw = np.full(PIXELS, 3, dtype=np.uint8).tobytes()
    ok, missing = validate_submission(raw)
    assert ok is True and missing == 0


def test_validate_flags_unfilled_count():
    arr = np.full(PIXELS, 1, dtype=np.uint8)
    arr[:7] = 255
    ok, missing = validate_submission(arr.tobytes())
    assert ok is False and missing == 7


@pytest.mark.parametrize("bad", [0, 7, 100, 254])
def test_validate_rejects_invalid_class_values(bad):
    arr = np.full(PIXELS, 1, dtype=np.uint8)
    arr[0] = bad
    with pytest.raises(ValueError):
        validate_submission(arr.tobytes())


def test_validate_rejects_wrong_size():
    with pytest.raises(ValueError):
        validate_submission(b"\x01" * 1024)


def test_validate_accepts_class_added_via_config(monkeypatch):
    """Adicionar uma 7ª classe ao config.yaml deve passar a aceitar valor=7
    sem mudança de código. Garante que `validate_partial` resolve os IDs em
    runtime via get_config() (ver mask_utils.py:46)."""
    import backend.config as config_mod
    fake_classes = [{"id": i, "name": f"c{i}", "color": "#000"} for i in range(1, 8)]
    monkeypatch.setattr(
        config_mod, "get_config",
        lambda: {"classes": fake_classes, "auth": {"jwt_secret": "x"}, "database": {"path": ":memory:"}},
    )
    arr = np.full(PIXELS, 7, dtype=np.uint8)
    ok, missing = validate_submission(arr.tobytes())
    assert ok is True and missing == 0


def test_validate_rejects_class_removed_via_config(monkeypatch):
    """Inversa: se a config remove uma classe, máscaras antigas com esse ID
    são rejeitadas pelo validate. Cobre o cenário de drift de YAML/banco."""
    import backend.config as config_mod
    # Apenas classes 1..3 ficam válidas; 4 deixou de existir.
    fake_classes = [{"id": i, "name": f"c{i}", "color": "#000"} for i in range(1, 4)]
    monkeypatch.setattr(
        config_mod, "get_config",
        lambda: {"classes": fake_classes, "auth": {"jwt_secret": "x"}, "database": {"path": ":memory:"}},
    )
    arr = np.full(PIXELS, 4, dtype=np.uint8)
    with pytest.raises(ValueError):
        validate_submission(arr.tobytes())
