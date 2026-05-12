"""Validation for classification-tile submissions. Sibling of mask_utils
(raster) and vector_utils (vector): a single class id per tile, payload is
`{"class_id": int}` JSON. All functions DB-free for editor-side reuse."""
from __future__ import annotations
import json


def parse_class_id(text: str, allowed_ids: list[int]) -> int:
    """Parse a classification submit body and return the class_id.

    Raises ValueError with a human-readable message on any structural problem
    or when the class id is not in `allowed_ids`."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("body vazio")
    try:
        doc = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"JSON inválido: {e}")
    if not isinstance(doc, dict):
        raise ValueError("esperado objeto JSON com class_id")
    cid = doc.get("class_id")
    if not isinstance(cid, int) or isinstance(cid, bool):
        raise ValueError("class_id precisa ser inteiro")
    if cid not in allowed_ids:
        raise ValueError(f"class_id {cid} fora das classes do projeto: {sorted(allowed_ids)}")
    return cid
