"""Vector-overlay rendering, thumbnails, and feature_distribution.

These exercise the kind-dispatch in mask_tile_service / admin/thumbnails /
admin/dashboard. Cache file naming and reuse are inherited from the raster
suite (per-project mbtiles in `cache_path(project_id)`)."""
import io
import json

import numpy as np
import pytest
from PIL import Image
from tests.conftest import token
from tests._vector_helpers import (
    auth as h,
    line_feature as _line,
    seed_classified_vector as _seed_classified_vector,
)
from tests._vector_helpers import create_vector_project as _create_vp_helper


def _create_vector_project(client, tok, tmp_path, name="hidro", attrs=None,
                            topology=False):
    # Overlay tests need maxzoom=18 to query at z=15+ realistically.
    return _create_vp_helper(
        client, tok, tmp_path,
        name=name, attributes=attrs, topology=topology, maxzoom=18,
    )


# ---- Overlay ---------------------------------------------------------------

def test_vector_overlay_renders_lines(client, admin_user, tmp_path):
    """The overlay must produce a non-transparent PNG when the project has
    classified vector tiles intersecting the requested WM tile."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="ov")
    # Tile spans (-50, -20.005) → (-49.99, -20). Drawing a line near the
    # tile's center; the WM tile at z=14 we'll fetch covers this area.
    bbox = (-50.0, -20.005760, -49.994240, -20.0)
    _seed_classified_vector(
        proj["id"], name="t1", bbox=bbox,
        features=[_line(
            [[-50.0, -20.0], [-49.994, -20.005]],
            tipo="rio",
        )],
    )
    from backend import mask_tile_service as mts
    z, x_min, y_min, _, _ = (14, *mts.wm_tiles_for_bbox(14, *bbox))
    png = mts.get_tile(proj["id"], z, x_min, y_min)
    assert png and png.startswith(b"\x89PNG")
    arr = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    # At least one pixel with nonzero alpha (line drawn).
    assert (arr[..., 3] > 0).any(), "expected some opaque pixel from the line"


def test_vector_overlay_empty_when_no_features(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="ov-empty")
    # Tile has no features → all-transparent PNG (cached as NULL after).
    from backend import mask_tile_service as mts
    png = mts.get_tile(proj["id"], 14, 100, 100)
    arr = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert (arr[..., 3] == 0).all()


def test_vector_overlay_color_picks_direction_when_attribute_present(client, admin_user, tmp_path):
    """Project with a `direction` attribute → overlay colors by direction
    (forward=blue, reverse=red, both=gray). The user can quickly distinguish
    flow direction in the admin map."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(
        client, tok, tmp_path, name="dir",
        attrs=[
            {"key": "direction", "type": "enum", "label": "Dir",
             "required": True, "options": ["forward", "reverse", "both"]},
        ],
        topology=True,
    )
    bbox = (-50.0, -20.005760, -49.994240, -20.0)
    _seed_classified_vector(
        proj["id"], name="t1", bbox=bbox,
        features=[_line(
            [[-49.999, -20.001], [-49.995, -20.004]],
            direction="forward",
        )],
    )
    from backend import mask_tile_service as mts
    z, x_min, y_min, _, _ = (14, *mts.wm_tiles_for_bbox(14, *bbox))
    png = mts.get_tile(proj["id"], z, x_min, y_min)
    arr = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    opaque = arr[..., 3] > 0
    # Forward → (55, 126, 184). Verify presence of a pixel near that color.
    blue = (
        (arr[..., 0] >= 50) & (arr[..., 0] <= 60)
        & (arr[..., 1] >= 120) & (arr[..., 1] <= 130)
        & (arr[..., 2] >= 180) & (arr[..., 2] <= 190)
        & opaque
    )
    assert blue.any(), "expected forward-direction blue pixels"


# ---- feature_distribution --------------------------------------------------

def test_feature_distribution_aggregates_enum_values(client, admin_user, tmp_path):
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="fd")
    bbox = (0.0, 0.0, 0.1, 0.1)
    _seed_classified_vector(
        proj["id"], name="t1", bbox=bbox,
        features=[
            _line([[0, 0], [0.05, 0.05]], tipo="rio"),
            _line([[0.05, 0.05], [0.08, 0.08]], tipo="rio"),
            _line([[0.05, 0.05], [0.08, 0.04]], tipo="arroio"),
        ],
    )
    body = client.get(
        f"/api/admin/feature-distribution?project_id={proj['id']}",
        headers=h(tok),
    ).json()
    counts = {(b["attribute_key"], b["value"]): b["count"] for b in body}
    assert counts.get(("tipo", "rio")) == 2
    assert counts.get(("tipo", "arroio")) == 1
    # Each entry's pct is over the per-(project, attribute) total, so they
    # sum to 100 within rounding for a single project + attribute.
    pcts = [b["pct"] for b in body if b["attribute_key"] == "tipo"]
    assert abs(sum(pcts) - 100.0) < 0.05


def test_feature_distribution_skips_text_and_number(client, admin_user, tmp_path):
    """Unbounded value spaces (text, number) are excluded — the chart only
    makes sense for discrete vocabularies."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(
        client, tok, tmp_path, name="fd-mix",
        attrs=[
            {"key": "tipo", "type": "enum", "label": "Tipo",
             "required": True, "options": ["a", "b"]},
            {"key": "lanes", "type": "number", "label": "Faixas"},
            {"key": "obs", "type": "text", "label": "Obs"},
        ],
    )
    _seed_classified_vector(
        proj["id"], name="t1", bbox=(0.0, 0.0, 0.1, 0.1),
        features=[_line([[0, 0], [1, 1]], tipo="a", lanes=2, obs="x")],
    )
    body = client.get(
        f"/api/admin/feature-distribution?project_id={proj['id']}",
        headers=h(tok),
    ).json()
    keys = {b["attribute_key"] for b in body}
    assert keys == {"tipo"}  # lanes/obs excluded


def test_feature_distribution_includes_boolean(client, admin_user, tmp_path):
    """Boolean attributes are countable — true/false are discrete."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(
        client, tok, tmp_path, name="fd-bool",
        attrs=[
            {"key": "iluminacao", "type": "boolean", "label": "Ilum"},
        ],
    )
    _seed_classified_vector(
        proj["id"], name="t1", bbox=(0.0, 0.0, 0.1, 0.1),
        features=[
            _line([[0, 0], [1, 1]], iluminacao=True),
            _line([[1, 0], [2, 1]], iluminacao=False),
            _line([[0, 1], [1, 2]], iluminacao=True),
        ],
    )
    body = client.get(
        f"/api/admin/feature-distribution?project_id={proj['id']}",
        headers=h(tok),
    ).json()
    by_value = {b["value"]: b["count"] for b in body if b["attribute_key"] == "iluminacao"}
    assert by_value == {"true": 2, "false": 1}


def test_feature_distribution_empty_for_raster_project(client, admin_user):
    """Raster projects don't contribute to feature_distribution at all
    (kind filter), so the default-project view is empty."""
    tok = token(client, admin_user["username"], admin_user["password"])
    body = client.get("/api/admin/feature-distribution?project_id=1", headers=h(tok)).json()
    assert body == []


# ---- Thumbnail dispatch ---------------------------------------------------

def test_thumbnail_dispatch_to_vector_renderer(client, admin_user, tmp_path):
    """For a vector tile, the thumbnail composites lines on the satellite
    backdrop (or dark background when satellite isn't available)."""
    tok = token(client, admin_user["username"], admin_user["password"])
    proj = _create_vector_project(client, tok, tmp_path, name="thumb")
    tid = _seed_classified_vector(
        proj["id"], name="t1", bbox=(0.0, 0.0, 0.1, 0.1),
        features=[_line([[0.01, 0.01], [0.09, 0.09]], tipo="rio")],
    )
    r = client.get(f"/api/admin/tiles/{tid}/thumbnail", headers=h(tok))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (128, 128)  # default thumb size
