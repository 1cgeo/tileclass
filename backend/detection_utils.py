"""Detection-tile body validation: GeoJSON FeatureCollection of axis-aligned
bounding boxes, one project class per box. Mirrors the role of `mask_utils.py`
(raster) and `vector_utils.py` (vector lines) for the detection flow.

A box is a GeoJSON Polygon whose outer ring is an axis-aligned rectangle
(exactly two distinct longitudes and two distinct latitudes). Each feature
carries `properties.class_id` referencing the project's palette.

All functions are pure and DB-free so the editor's pre-submit check can share
the exact rules the backend runs."""
from __future__ import annotations
import json
from collections import Counter
from typing import Any


def box_count(text: str | None) -> int:
    """Cheap count of boxes in a FeatureCollection blob. 0 for empty/None/parse
    errors so callers can treat it as a cache value (feature_count column)."""
    if not text:
        return 0
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return 0
    feats = doc.get("features") if isinstance(doc, dict) else None
    return len(feats) if isinstance(feats, list) else 0


def _validate_box(f: Any, idx: int) -> None:
    if not isinstance(f, dict) or f.get("type") != "Feature":
        raise ValueError(f"feature[{idx}]: type=Feature obrigatório")
    geom = f.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        raise ValueError(f"feature[{idx}]: geometry deve ser Polygon (bbox)")
    rings = geom.get("coordinates")
    if not isinstance(rings, list) or not rings:
        raise ValueError(f"feature[{idx}]: Polygon.coordinates vazio")
    # A bounding box is a single ring — reject polygons with holes/extra rings
    # so a malformed multi-ring body can't slip through as a "box".
    if len(rings) != 1:
        raise ValueError(f"feature[{idx}]: caixa deve ter exatamente 1 anel (sem furos)")
    ring = rings[0]
    if not isinstance(ring, list) or len(ring) < 4:
        raise ValueError(f"feature[{idx}]: anel precisa de ≥4 vértices")
    xs, ys = set(), set()
    for j, c in enumerate(ring):
        if not (isinstance(c, list) and len(c) >= 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in c[:2])):
            raise ValueError(f"feature[{idx}].coordinates[{j}]: par [lng, lat] esperado")
        xs.add(round(float(c[0]), 9))
        ys.add(round(float(c[1]), 9))
    # Axis-aligned rectangle ⇔ exactly two distinct x and two distinct y.
    if len(xs) != 2 or len(ys) != 2:
        raise ValueError(
            f"feature[{idx}]: caixa deve ser retângulo alinhado aos eixos "
            f"(2 longitudes e 2 latitudes distintas)"
        )
    props = f.get("properties")
    if props is not None and not isinstance(props, dict):
        raise ValueError(f"feature[{idx}].properties: precisa ser objeto ou null")


def parse_detection(text: str) -> dict:
    """Parse + structurally validate a FeatureCollection of axis-aligned box
    Polygons. Raises ValueError with a user-facing message on any problem.
    Does NOT check class_id membership (needs the project palette) — that's
    `validate_class_ids`."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("body vazio")
    try:
        doc = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"GeoJSON inválido: {e}")
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        raise ValueError("esperado type=FeatureCollection no topo")
    feats = doc.get("features")
    if not isinstance(feats, list):
        raise ValueError("FeatureCollection.features precisa ser uma lista")
    for i, f in enumerate(feats):
        _validate_box(f, i)
    return doc


def validate_class_ids(features: list[dict], allowed_ids: list[int]) -> list[str]:
    """Each box's properties.class_id must be an int in the project palette.
    Returns a list of human-readable errors; empty when OK."""
    errors: list[str] = []
    allowed = set(allowed_ids)
    for i, f in enumerate(features):
        cid = (f.get("properties") or {}).get("class_id")
        if not isinstance(cid, int) or isinstance(cid, bool):
            errors.append(f"feature[{i}]: class_id inteiro obrigatório")
        elif cid not in allowed:
            errors.append(f"feature[{i}].class_id={cid} fora da paleta {sorted(allowed)}")
    return errors


def class_counts(doc_or_text: Any) -> dict[str, int]:
    """Box counts per class id, as {str(class_id): count} — same JSON shape as
    the raster pixel-count cache so the dashboard aggregates uniformly."""
    if isinstance(doc_or_text, str):
        try:
            doc = json.loads(doc_or_text)
        except (TypeError, ValueError):
            return {}
    else:
        doc = doc_or_text or {}
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        return {}
    counter: Counter = Counter()
    for f in feats:
        cid = (f.get("properties") or {}).get("class_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            counter[str(cid)] += 1
    return dict(counter)


def validate_submission(text: str, allowed_ids: list[int], *,
                        box_required: bool = False) -> tuple[bool, dict]:
    """End-to-end validation. Returns (ok, payload) where payload always has
    'errors' and — when parsing succeeded — 'doc' (the parsed FeatureCollection).
    Order: structural (parse_detection) → class_ids → box_required gate."""
    try:
        doc = parse_detection(text)
    except ValueError as e:
        return False, {"errors": [str(e)]}
    feats = doc["features"]
    errs = validate_class_ids(feats, allowed_ids)
    if box_required and not feats:
        errs.append("tile precisa de ao menos uma caixa")
    if errs:
        return False, {"errors": errs}
    return True, {"errors": [], "doc": doc}


def empty_feature_collection() -> str:
    """Canonical empty body — initial state for new tiles so the editor always
    loads valid JSON."""
    return '{"type":"FeatureCollection","features":[]}'
