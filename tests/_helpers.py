"""Shared helpers for the export test suites (test_export.py,
test_export_api.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path


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
