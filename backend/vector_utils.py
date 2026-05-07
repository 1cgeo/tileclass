"""Vector-tile body validation: GeoJSON parsing, attribute schema enforcement,
and (optional) topology checks for graph-style projects (drainage, road
networks). Mirrors the role of `mask_utils.py` for raster tiles.

All functions are pure and DB-free so the editor's pre-submit check can
share the exact rules the backend will run."""
from __future__ import annotations
import json
import math
from typing import Any


# ---- Constants -------------------------------------------------------------

# Snap tolerance for endpoint connectivity checks. Tile pixels are 2.5 m on
# the ground; we snap within ~1.25 m (half a pixel), expressed in degrees at
# any latitude (~1e-5 deg ≈ 1.1 m at the equator; less near the poles, but
# tile_meters is constant via geo.bbox_from_center so the tolerance is good
# enough across latitudes).
SNAP_TOLERANCE_DEG = 1.5e-5

ALLOWED_ATTR_TYPES = ("text", "number", "enum", "boolean")
ALLOWED_DIRECTIONS = ("forward", "reverse", "both")


# ---- Public API ------------------------------------------------------------

def feature_count(text: str | None) -> int:
    """Cheap count of features in a GeoJSON FeatureCollection blob.
    0 for empty/None/parse errors so callers can treat it as a cache value."""
    if not text:
        return 0
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return 0
    feats = doc.get("features") if isinstance(doc, dict) else None
    return len(feats) if isinstance(feats, list) else 0


def parse_geojson(text: str) -> dict:
    """Parse + structurally validate a FeatureCollection of LineStrings.
    Raises ValueError with a user-facing message on any structural problem."""
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
        _validate_feature(f, i)
    return doc


def _validate_feature(f: Any, idx: int) -> None:
    if not isinstance(f, dict) or f.get("type") != "Feature":
        raise ValueError(f"feature[{idx}]: type=Feature obrigatório")
    geom = f.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "LineString":
        raise ValueError(f"feature[{idx}]: geometry deve ser LineString")
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ValueError(
            f"feature[{idx}]: LineString precisa de ≥2 vértices, tem {len(coords) if isinstance(coords, list) else 0}"
        )
    for j, c in enumerate(coords):
        if not (isinstance(c, list) and len(c) >= 2
                and all(isinstance(v, (int, float)) for v in c[:2])):
            raise ValueError(f"feature[{idx}].coordinates[{j}]: par [lng, lat] esperado")
    props = f.get("properties")
    if props is not None and not isinstance(props, dict):
        raise ValueError(f"feature[{idx}].properties: precisa ser objeto ou null")


def validate_attributes(features: list[dict], schema: list[dict]) -> list[str]:
    """Check each feature's properties against the project's attribute
    schema. Returns a list of human-readable errors; empty when OK.
    Schema rows are dicts with at least {key, type, required, options}."""
    errors: list[str] = []
    for i, f in enumerate(features):
        props = f.get("properties") or {}
        for attr in schema:
            key = attr["key"]
            t = attr["type"]
            req = bool(attr.get("required"))
            value = props.get(key)
            if value is None or value == "":
                if req:
                    errors.append(f"feature[{i}]: atributo '{key}' é obrigatório")
                continue
            if t == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"feature[{i}].{key}: número esperado")
            elif t == "boolean":
                if not isinstance(value, bool):
                    errors.append(f"feature[{i}].{key}: boolean esperado")
            elif t == "enum":
                opts = attr.get("options") or []
                if value not in opts:
                    errors.append(
                        f"feature[{i}].{key}: '{value}' fora de {opts}"
                    )
            elif t == "text":
                if not isinstance(value, str):
                    errors.append(f"feature[{i}].{key}: texto esperado")
    return errors


def validate_topology(features: list[dict]) -> list[str]:
    """Topology checks for graph-style vector projects:
       - every LineString carries `direction ∈ {forward, reverse, both}` in
         properties (forward/reverse identify edge orientation; `both` for
         undirected segments);
       - endpoints close to other endpoints (within SNAP_TOLERANCE_DEG) are
         considered the same node — connectivity is implicit;
       - no cycles within the tile (simple DFS over the implicit node graph).
    Returns a list of error strings; empty when OK."""
    errors: list[str] = []
    if not features:
        return errors

    # 1. Direction property check.
    for i, f in enumerate(features):
        d = (f.get("properties") or {}).get("direction")
        if d not in ALLOWED_DIRECTIONS:
            errors.append(
                f"feature[{i}].direction: {ALLOWED_DIRECTIONS} esperado, "
                f"recebido {d!r}"
            )
    if errors:
        return errors  # Bail early; cycle check below assumes valid features.

    # 2. Build node graph by snapping endpoints to a canonical bucket.
    nodes: list[tuple[float, float]] = []  # canonical positions
    edges: list[tuple[int, int, str]] = []  # (from_node, to_node, direction)

    def _snap(point: tuple[float, float]) -> int:
        for nid, n in enumerate(nodes):
            if (abs(point[0] - n[0]) < SNAP_TOLERANCE_DEG
                    and abs(point[1] - n[1]) < SNAP_TOLERANCE_DEG):
                return nid
        nodes.append(point)
        return len(nodes) - 1

    for f in features:
        coords = f["geometry"]["coordinates"]
        a = _snap((coords[0][0], coords[0][1]))
        b = _snap((coords[-1][0], coords[-1][1]))
        if a == b:
            errors.append(
                "feature termina no mesmo nó em que começa (loop de 1 aresta)"
            )
            continue
        edges.append((a, b, (f.get("properties") or {}).get("direction", "both")))

    # 3. Cycle detection following the DAG induced by `forward` edges
    # (a→b for forward, b→a for reverse, both directions for `both`).
    if errors:
        return errors

    adj: dict[int, list[int]] = {i: [] for i in range(len(nodes))}
    for a, b, d in edges:
        if d == "forward":
            adj[a].append(b)
        elif d == "reverse":
            adj[b].append(a)
        else:  # 'both' — both directions
            adj[a].append(b)
            adj[b].append(a)

    color = [0] * len(nodes)  # 0=unseen, 1=on stack, 2=done

    def _has_cycle(start: int) -> bool:
        stack = [(start, iter(adj[start]))]
        color[start] = 1
        while stack:
            node, it = stack[-1]
            try:
                nxt = next(it)
            except StopIteration:
                color[node] = 2
                stack.pop()
                continue
            if color[nxt] == 1:
                return True
            if color[nxt] == 0:
                color[nxt] = 1
                stack.append((nxt, iter(adj[nxt])))
        return False

    for n in range(len(nodes)):
        if color[n] == 0 and _has_cycle(n):
            errors.append("ciclo detectado no grafo (drenagem precisa ser acíclica)")
            break
    return errors


def validate_submission(text: str, schema: list[dict], *,
                        topology_required: bool = False) -> tuple[bool, dict]:
    """End-to-end validation. Returns (ok, {'errors': [...]}) where errors
    is a flat list of user-facing strings. The order is: structural (raises
    early via parse_geojson) → attributes → topology."""
    try:
        doc = parse_geojson(text)
    except ValueError as e:
        return False, {"errors": [str(e)]}
    feats = doc["features"]
    errs = validate_attributes(feats, schema)
    if topology_required:
        errs.extend(validate_topology(feats))
    return (not errs), {"errors": errs}


def empty_feature_collection() -> str:
    """Canonical empty body — used as the initial state for new tiles so
    the editor always loads valid JSON."""
    return '{"type":"FeatureCollection","features":[]}'
