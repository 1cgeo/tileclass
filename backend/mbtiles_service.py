"""MBTiles tile server (read-only).

The SQLite DB is opened read-only + immutable, one connection per worker
thread (FastAPI runs sync endpoints in a thread pool, and a single sqlite3
connection cannot serve concurrent queries even with check_same_thread=False).
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from typing import Optional

_local = threading.local()
_path: Optional[Path] = None
_format: str = "webp"
_min_zoom: Optional[int] = None
_max_zoom: Optional[int] = None


def _uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro&immutable=1"


def open_mbtiles(path: Path) -> None:
    """Record the path and cache metadata; per-thread connections open lazily."""
    global _path, _format, _min_zoom, _max_zoom
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"mbtiles not found: {p}")
    _path = p
    conn = sqlite3.connect(_uri(p), uri=True)
    try:
        meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
    finally:
        conn.close()
    _format = (meta.get("format") or "webp").lower()
    try:
        _min_zoom = int(meta["minzoom"]) if "minzoom" in meta else None
        _max_zoom = int(meta["maxzoom"]) if "maxzoom" in meta else None
    except (ValueError, TypeError):
        _min_zoom = _max_zoom = None


def close_mbtiles() -> None:
    """Release the path so is_open() → False. Thread-local connections
    are cleaned up when their threads terminate (FastAPI threadpool lifecycle)."""
    global _path
    _path = None


def is_open() -> bool:
    return _path is not None


def _thread_conn() -> Optional[sqlite3.Connection]:
    if _path is None:
        return None
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(_uri(_path), uri=True)
        _local.conn = c
    return c


def get_tile(z: int, x: int, y: int) -> Optional[bytes]:
    """XYZ lookup; returns raw tile bytes or None if absent/out of range."""
    if _min_zoom is not None and z < _min_zoom:
        return None
    if _max_zoom is not None and z > _max_zoom:
        return None
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        return None
    conn = _thread_conn()
    if conn is None:
        return None
    tms_y = (n - 1) - y
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
        (z, x, tms_y),
    ).fetchone()
    return row[0] if row else None


def tile_format() -> str:
    return _format


def zoom_range() -> tuple[Optional[int], Optional[int]]:
    return _min_zoom, _max_zoom
