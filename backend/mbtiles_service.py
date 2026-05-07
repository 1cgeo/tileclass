"""MBTiles tile readers (read-only), pooled per (project_id, layer).

Each project declares up to five mbtiles paths (`primary`, `secondary`,
`tertiary`, `ref_primary`, `ref_secondary`). Readers are opened lazily on
the first request, cached for reuse, and bounded by an LRU so a server
hosting many projects does not exhaust file descriptors.

The SQLite DB is opened read-only + immutable, one connection per worker
thread (FastAPI runs sync endpoints in a thread pool, and a single sqlite3
connection cannot serve concurrent queries even with check_same_thread=False).
"""
from __future__ import annotations
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from . import project_service


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
        """Release the path so is_open() → False."""
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


# ---- Reader pool ------------------------------------------------------------

_DEFAULT_FORMATS = {
    "primary": "webp",
    "secondary": "webp",
    "tertiary": "webp",
    "ref_primary": "png",
    "ref_secondary": "png",
}

# Bounded LRU: each open mbtiles holds one fd per worker thread, so 32 entries
# × ~16 threads × 5 layers fits comfortably under typical fd limits (1024+).
_MAX_OPEN = 32

_pool_lock = threading.Lock()
_pool: "OrderedDict[tuple[int, str], MBTilesReader]" = OrderedDict()


def _close_evicted(reader: MBTilesReader) -> None:
    try:
        reader.close()
    except Exception:
        pass


def get_reader(project_id: int, layer: str) -> Optional[MBTilesReader]:
    """Return an opened reader for (project_id, layer), or None when the
    project has no path configured for that layer or the file is missing.
    Idempotent and thread-safe."""
    if layer not in _DEFAULT_FORMATS:
        return None
    key = (project_id, layer)
    with _pool_lock:
        existing = _pool.get(key)
        if existing is not None:
            _pool.move_to_end(key)
            if existing.is_open():
                return existing
    proj = project_service.get_project(project_id)
    if not proj:
        return None
    column = project_service._LAYER_COLUMN[layer]
    path = project_service.resolve_mbtiles_path(proj.get(column))
    if path is None or not path.exists():
        return None
    reader = MBTilesReader(_DEFAULT_FORMATS[layer])
    try:
        reader.open(path)
    except (FileNotFoundError, sqlite3.DatabaseError, sqlite3.OperationalError):
        # File exists but isn't a valid mbtiles (test stubs, half-downloaded
        # files, corrupt). Treat as "not configured" so callers can render a
        # configured-but-not-open status without crashing the request.
        return None
    with _pool_lock:
        _pool[key] = reader
        _pool.move_to_end(key)
        while len(_pool) > _MAX_OPEN:
            _, evicted = _pool.popitem(last=False)
            _close_evicted(evicted)
    return reader


def invalidate_project(project_id: int) -> None:
    """Drop all cached readers for the given project. Called after a project
    update changes any mbtiles path."""
    with _pool_lock:
        for key in list(_pool.keys()):
            if key[0] == project_id:
                _close_evicted(_pool.pop(key))


def close_all() -> None:
    with _pool_lock:
        for r in _pool.values():
            _close_evicted(r)
        _pool.clear()


def open_readers() -> dict:
    """Snapshot of currently-pooled readers for the maintenance dashboard."""
    with _pool_lock:
        items = [(pid, layer, r) for (pid, layer), r in _pool.items()]
    out = {}
    for pid, layer, r in items:
        out[f"{pid}:{layer}"] = {
            "open": r.is_open(),
            "format": r.tile_format(),
            "min_zoom": r.zoom_range()[0],
            "max_zoom": r.zoom_range()[1],
            "path": str(r.path()) if r.path() else None,
        }
    return out
