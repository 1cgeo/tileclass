"""MBTiles tile readers (read-only).

The SQLite DB is opened read-only + immutable, one connection per worker
thread (FastAPI runs sync endpoints in a thread pool, and a single sqlite3
connection cannot serve concurrent queries even with check_same_thread=False).

Three pre-instantiated singletons are exported: `primary` (the satellite/imagery
mbtiles, default WebP), `worldcover` (the WC overlay, PNG), and `mapbiomas`
(the MapBiomas overlay, PNG, mapped to TileClass palette).
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from typing import Optional


class MBTilesReader:
    def __init__(self, default_format: str = "webp") -> None:
        self._local = threading.local()
        self._path: Optional[Path] = None
        self._format = default_format
        self._min_zoom: Optional[int] = None
        self._max_zoom: Optional[int] = None

    def _uri(self, path: Path) -> str:
        return f"file:{path.as_posix()}?mode=ro&immutable=1"

    def open(self, path: Path) -> None:
        """Record the path and cache metadata; per-thread connections open lazily."""
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"mbtiles not found: {p}")
        self._path = p
        conn = sqlite3.connect(self._uri(p), uri=True)
        try:
            meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
        finally:
            conn.close()
        self._format = (meta.get("format") or self._format).lower()
        try:
            self._min_zoom = int(meta["minzoom"]) if "minzoom" in meta else None
            self._max_zoom = int(meta["maxzoom"]) if "maxzoom" in meta else None
        except (ValueError, TypeError):
            self._min_zoom = self._max_zoom = None

    def close(self) -> None:
        """Release the path so is_open() → False. Thread-local connections
        are cleaned up when their threads terminate (FastAPI threadpool lifecycle)."""
        self._path = None

    def is_open(self) -> bool:
        return self._path is not None

    def _thread_conn(self) -> Optional[sqlite3.Connection]:
        if self._path is None:
            return None
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._uri(self._path), uri=True)
            self._local.conn = c
        return c

    def get_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """XYZ lookup; returns raw tile bytes or None if absent/out of range."""
        if self._min_zoom is not None and z < self._min_zoom:
            return None
        if self._max_zoom is not None and z > self._max_zoom:
            return None
        n = 1 << z
        if x < 0 or x >= n or y < 0 or y >= n:
            return None
        conn = self._thread_conn()
        if conn is None:
            return None
        tms_y = (n - 1) - y
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
            (z, x, tms_y),
        ).fetchone()
        return row[0] if row else None

    def tile_format(self) -> str:
        return self._format

    def zoom_range(self) -> tuple[Optional[int], Optional[int]]:
        return self._min_zoom, self._max_zoom

    def path(self) -> Optional[Path]:
        return self._path


primary = MBTilesReader("webp")
worldcover = MBTilesReader("png")
mapbiomas = MBTilesReader("png")
