"""Strong mask persistence tests: byte-exact round-trip with non-uniform patterns.
Weak tests that only submit fill=1 would pass even if encode_mask returned
a constant PNG. These use patterns that catch that class of bugs."""
import numpy as np
from tests.conftest import token


def h(t): return {"Authorization": f"Bearer {t}"}


def test_classify_stores_byte_exact_checkerboard(client, operators, tiles):
    """Checkerboard of classes 1 and 2 → after classify, image decodes to same bytes."""
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()

    arr = np.zeros((256, 256), dtype=np.uint8)
    arr[::2, ::2] = 1
    arr[1::2, 1::2] = 1
    arr[::2, 1::2] = 2
    arr[1::2, ::2] = 2
    payload = arr.tobytes()

    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=payload)
    assert r.status_code == 200

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t)).content
    decoded = decode_mask(img)
    assert decoded == payload, "Mask bytes diverge after classify round-trip"


def test_classify_stores_byte_exact_all_six_classes(client, operators, tiles):
    """6 horizontal stripes, one per class, tests every valid value."""
    from backend.mask_utils import decode_mask
    t = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t)).json()

    arr = np.zeros((256, 256), dtype=np.uint8)
    band = 256 // 6
    for cls in range(1, 7):
        arr[(cls - 1) * band:cls * band, :] = cls
    # Last remainder rows → class 6
    arr[6 * band:, :] = 6
    payload = arr.tobytes()

    r = client.post(f"/api/tiles/{tile['id']}/classify",
                    headers={**h(t), "Content-Type": "application/octet-stream"},
                    content=payload)
    assert r.status_code == 200

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t)).content
    assert decode_mask(img) == payload


def test_review_overwrites_with_different_pattern(client, operators, tiles):
    """Classifier writes pattern A, reviewer writes pattern B. Final = B, exactly."""
    from backend.mask_utils import decode_mask
    t1 = token(client, "op1", "secret123")
    tile = client.get("/api/tiles/next", headers=h(t1)).json()

    pattern_a = np.full(65536, 3, dtype=np.uint8)
    pattern_a[:1000] = 4
    client.post(f"/api/tiles/{tile['id']}/classify",
                headers={**h(t1), "Content-Type": "application/octet-stream"},
                content=pattern_a.tobytes())

    t2 = token(client, "op2", "secret123")
    client.get("/api/tiles/next", headers=h(t2))

    pattern_b = np.full(65536, 5, dtype=np.uint8)
    pattern_b[64000:] = 6
    client.post(f"/api/tiles/{tile['id']}/review",
                headers={**h(t2), "Content-Type": "application/octet-stream"},
                content=pattern_b.tobytes())

    img = client.get(f"/api/tiles/{tile['id']}/image", headers=h(t2)).content
    assert decode_mask(img) == pattern_b.tobytes()
    assert decode_mask(img) != pattern_a.tobytes()
