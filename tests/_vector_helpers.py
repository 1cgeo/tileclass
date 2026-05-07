"""Shared fixtures for the vector-projects test suite.

Each helper used to live duplicated across test_vector_projects.py,
test_vector_overlay.py, and test_vector_export.py. Centralizing here
keeps the contract one-version (mbtiles schema, project payload shape,
tile insert columns) so a backend change updates one place."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_real_mbtiles(tmp_path: Path, name: str = "real.mbtiles",
                      fmt: str = "png", maxzoom: int = 18) -> str:
    """Write the minimum-viable mbtiles SQLite skeleton mbtiles_service
    expects (metadata + empty tiles table). Returns the path as a string."""
    path = tmp_path / name
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INT, tile_column INT, tile_row INT,"
        " tile_data BLOB, PRIMARY KEY(zoom_level, tile_column, tile_row));"
    )
    conn.execute("INSERT INTO metadata VALUES('format',?)", (fmt,))
    conn.execute(
        "INSERT INTO metadata VALUES('minzoom','0'),('maxzoom',?)", (str(maxzoom),)
    )
    conn.commit()
    conn.close()
    return str(path)


_DEFAULT_ATTRS = [
    {"key": "tipo", "type": "enum", "label": "Tipo",
     "required": True, "options": ["rio", "arroio"]},
]


def vector_project_body(tmp_path: Path, *, name: str,
                        attributes: list[dict] | None = None,
                        topology: bool = False, maxzoom: int = 3) -> dict:
    """The JSON body for POST /api/admin/projects with kind='vector'."""
    return {
        "name": name,
        "kind": "vector",
        "topology_required": topology,
        "primary_mbtiles": make_real_mbtiles(
            tmp_path, f"{name}.mbtiles", maxzoom=maxzoom,
        ),
        "attributes": _DEFAULT_ATTRS if attributes is None else attributes,
    }


def create_vector_project(client, tok: str, tmp_path: Path, *,
                          name: str = "hidro", **kw) -> dict:
    """POST a vector project and assert success. Returns the API response dict."""
    r = client.post(
        "/api/admin/projects",
        json=vector_project_body(tmp_path, name=name, **kw),
        headers=auth(tok),
    )
    assert r.status_code == 200, r.text
    return r.json()


def line_feature(coords: list[list[float]], **props: Any) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def seed_classified_vector(project_id: int, *, name: str,
                           bbox: tuple[float, float, float, float],
                           features: list[dict]) -> int:
    """Insert a tile with a pre-built FeatureCollection in 'classified' state.
    Used by overlay/distribution tests that need data without round-tripping
    through the editor's submit flow."""
    from backend.database import connect
    body = json.dumps({"type": "FeatureCollection", "features": features})
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status,
                                 data_geojson, feature_count, classified_at)
               VALUES (?,?,?,?,?,?,'classified',?,?,datetime('now'))""",
            (project_id, name, *bbox, body, len(features)),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
