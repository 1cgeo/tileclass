"""vector_utils: pure GeoJSON / attribute / topology validation.

These tests run without any DB or HTTP — they pin the contract the editor
mirrors client-side."""
import json

import pytest

from backend.vector_utils import (
    SNAP_TOLERANCE_DEG,
    empty_feature_collection,
    feature_count,
    parse_geojson,
    validate_attributes,
    validate_submission,
    validate_topology,
)


def _line(coords, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def _fc(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


# ---- feature_count ---------------------------------------------------------

def test_feature_count_handles_empty_and_corrupt():
    assert feature_count(None) == 0
    assert feature_count("") == 0
    assert feature_count("not json") == 0
    assert feature_count('{"type":"X"}') == 0  # missing features
    assert feature_count(empty_feature_collection()) == 0
    assert feature_count(_fc(_line([[0, 0], [1, 1]]))) == 1
    assert feature_count(_fc(_line([[0, 0], [1, 1]]), _line([[2, 2], [3, 3]]))) == 2


# ---- parse_geojson ---------------------------------------------------------

def test_parse_geojson_rejects_non_feature_collection():
    with pytest.raises(ValueError, match="FeatureCollection"):
        parse_geojson('{"type":"Polygon","coordinates":[]}')


def test_parse_geojson_rejects_non_linestring_geometry():
    body = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {},
        }],
    })
    with pytest.raises(ValueError, match="LineString"):
        parse_geojson(body)


def test_parse_geojson_rejects_short_linestring():
    with pytest.raises(ValueError, match="≥2 vértices"):
        parse_geojson(_fc(_line([[0, 0]])))


def test_parse_geojson_rejects_malformed_coords():
    body = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], ["x", "y"]]},
            "properties": {},
        }],
    })
    with pytest.raises(ValueError, match="par .lng, lat."):
        parse_geojson(body)


def test_parse_geojson_accepts_valid():
    doc = parse_geojson(_fc(_line([[0, 0], [1, 1]])))
    assert len(doc["features"]) == 1


# ---- validate_attributes ---------------------------------------------------

_SCHEMA = [
    {"key": "pavimento", "type": "enum", "required": True,
     "options": ["asfalto", "terra"]},
    {"key": "lanes", "type": "number", "required": True},
    {"key": "iluminacao", "type": "boolean", "required": False},
    {"key": "obs", "type": "text", "required": False},
]


def test_attributes_required_missing():
    feats = [{"properties": {"lanes": 2}}]
    errs = validate_attributes(feats, _SCHEMA)
    assert any("pavimento" in e for e in errs)


def test_attributes_enum_out_of_options():
    feats = [{"properties": {"pavimento": "concreto", "lanes": 2}}]
    errs = validate_attributes(feats, _SCHEMA)
    assert any("concreto" in e for e in errs)


def test_attributes_number_type_check():
    feats = [{"properties": {"pavimento": "asfalto", "lanes": "2"}}]
    errs = validate_attributes(feats, _SCHEMA)
    assert any("número esperado" in e for e in errs)


def test_attributes_boolean_rejects_truthy_string():
    """Boolean must be a real bool — 'true' string slipping through would
    silently convert via JS truthiness on the client."""
    feats = [{"properties": {"pavimento": "asfalto", "lanes": 2,
                              "iluminacao": "true"}}]
    errs = validate_attributes(feats, _SCHEMA)
    assert any("boolean esperado" in e for e in errs)


def test_attributes_optional_can_be_absent():
    feats = [{"properties": {"pavimento": "asfalto", "lanes": 2}}]
    assert validate_attributes(feats, _SCHEMA) == []


def test_attributes_empty_string_treated_as_absent():
    """Required field with empty-string value is missing, not a typed error."""
    feats = [{"properties": {"pavimento": "", "lanes": 2}}]
    errs = validate_attributes(feats, _SCHEMA)
    assert any("obrigatório" in e for e in errs)


# ---- validate_topology -----------------------------------------------------

def test_topology_requires_direction_property():
    feats = [_line([[0, 0], [1, 1]])]  # no direction
    errs = validate_topology(feats)
    assert any("direction" in e for e in errs)


def test_topology_accepts_chain_no_cycle():
    feats = [
        _line([[0, 0], [1, 1]], direction="forward"),
        _line([[1, 1], [2, 2]], direction="forward"),
        _line([[2, 2], [3, 3]], direction="forward"),
    ]
    assert validate_topology(feats) == []


def test_topology_detects_simple_cycle():
    """A → B → C → A with all forward edges is a cycle."""
    feats = [
        _line([[0, 0], [1, 0]], direction="forward"),
        _line([[1, 0], [1, 1]], direction="forward"),
        _line([[1, 1], [0, 0]], direction="forward"),
    ]
    errs = validate_topology(feats)
    assert any("ciclo" in e.lower() for e in errs)


def test_topology_snaps_endpoints_within_tolerance():
    """Two segments with endpoints within tolerance share a node, so the
    chain is recognized (not flagged as disconnected)."""
    eps = SNAP_TOLERANCE_DEG / 2
    feats = [
        _line([[0, 0], [1.0, 1.0]], direction="forward"),
        _line([[1.0 + eps, 1.0 - eps], [2.0, 2.0]], direction="forward"),
    ]
    assert validate_topology(feats) == []


def test_topology_does_not_snap_outside_tolerance():
    """Two segments separated by > tolerance form 4 distinct nodes —
    still no cycle, no error."""
    far = SNAP_TOLERANCE_DEG * 100
    feats = [
        _line([[0, 0], [1.0, 1.0]], direction="forward"),
        _line([[1.0 + far, 1.0], [2.0, 2.0]], direction="forward"),
    ]
    assert validate_topology(feats) == []


def test_topology_self_loop_flagged():
    """A LineString that ends where it starts (self-loop) is flagged."""
    feats = [_line([[0, 0], [1, 1], [0, 0]], direction="forward")]
    errs = validate_topology(feats)
    assert any("loop" in e or "ciclo" in e.lower() for e in errs)


def test_topology_undirected_both_walks_two_ways():
    """`direction=both` makes the edge bidirectional — A↔B↔A in cycle check."""
    feats = [
        _line([[0, 0], [1, 0]], direction="both"),
        _line([[1, 0], [0, 0]], direction="forward"),
    ]
    errs = validate_topology(feats)
    assert any("ciclo" in e.lower() for e in errs)


def test_topology_accepts_empty():
    assert validate_topology([]) == []


# ---- validate_submission (end-to-end) --------------------------------------

def test_submission_returns_ok_for_clean_input():
    body = _fc(_line(
        [[0, 0], [1, 1]], pavimento="asfalto", lanes=2,
    ))
    ok, payload = validate_submission(body, _SCHEMA, topology_required=False)
    assert ok is True
    assert payload["errors"] == []
    # Parsed doc piggy-backs on success so callers don't re-parse for a count.
    assert payload["doc"]["features"][0]["properties"]["pavimento"] == "asfalto"


def test_submission_aggregates_errors():
    """Attribute and topology errors come back together in one response."""
    body = _fc(
        _line([[0, 0], [1, 1]]),  # missing direction + missing required attrs
    )
    ok, payload = validate_submission(body, _SCHEMA, topology_required=True)
    assert ok is False
    # Attribute errors run before topology; both surface.
    assert any("pavimento" in e for e in payload["errors"])


def test_submission_short_circuits_on_structural_error():
    """If GeoJSON is structurally broken, downstream checks aren't run —
    the editor would be misleading the user otherwise."""
    ok, payload = validate_submission("{not json}", _SCHEMA)
    assert ok is False and len(payload["errors"]) == 1
    assert "GeoJSON" in payload["errors"][0]
