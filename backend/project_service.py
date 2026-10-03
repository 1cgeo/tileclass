"""Project CRUD + membership + class config.

Read paths cache aggressively (`get_project` is hit on every editor load and
every layer URL build); mutations invalidate per-project cache keys plus the
mbtiles reader pool."""
from __future__ import annotations
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from .config import validate_classes
from .database import connect, transaction, log_action, now_iso


# mbtiles paths are stored relative to backend/ by convention; absolute
# paths pass through unchanged.
_BACKEND_ROOT = Path(__file__).resolve().parent

LAYER_KEYS = ("primary", "secondary", "tertiary", "ref_primary", "ref_secondary")
LAYER_COLUMN = {
    "primary": "primary_mbtiles",
    "secondary": "secondary_mbtiles",
    "tertiary": "tertiary_mbtiles",
    "ref_primary": "ref_mask_primary_mbtiles",
    "ref_secondary": "ref_mask_secondary_mbtiles",
}


def layer_path(project: dict, layer: str) -> str | None:
    """Stored layer source for `layer` on a project — either an mbtiles path
    (file) or a tile-server URL template (e.g. Martin / TileServer-GL).
    Single point of truth for the layer → column mapping."""
    column = LAYER_COLUMN.get(layer)
    return project.get(column) if column else None


_REMOTE_SCHEMES = ("http://", "https://", "bingmaps://")


def is_remote_layer(value: str | None) -> bool:
    """A layer source is treated as a remote tile server (fetched directly by
    the frontend, not file-backed) when it uses a remote scheme: http(s):// for
    standard XYZ servers, or bingmaps:// for the Bing quadkey scheme that
    maplib.js rewrites on the fly. Otherwise it's a local mbtiles file path."""
    return bool(value) and value.startswith(_REMOTE_SCHEMES)


def resolve_mbtiles_path(stored: str | None) -> Optional[Path]:
    """Resolve an mbtiles path: relative entries become `backend/<path>`.
    Returns None for remote URLs (those are not file-backed)."""
    if not stored or is_remote_layer(stored):
        return None
    p = Path(stored)
    if not p.is_absolute():
        p = _BACKEND_ROOT / p
    return p


# ---- Cache ------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_PROJECT_CACHE: dict[int, dict] = {}


def _invalidate(project_id: int | None = None, *, drop_overlay_cache: bool = False) -> None:
    """Drop the project LRU + mbtiles reader pool. Pass
    `drop_overlay_cache=True` when palette/layer-source changed —
    the admin overlay caches the rendered PNG (already with the palette
    baked in), so it doesn't refresh from per-tile invalidation alone."""
    with _CACHE_LOCK:
        if project_id is None:
            _PROJECT_CACHE.clear()
        else:
            _PROJECT_CACHE.pop(project_id, None)
    # Imports are lazy: mbtiles_service and mask_tile_service both import
    # project_service for path resolution / palette lookups.
    try:
        from . import mbtiles_service
        if project_id is None:
            mbtiles_service.close_all()
        else:
            mbtiles_service.invalidate_project(project_id)
    except Exception:
        pass
    if drop_overlay_cache and project_id is not None:
        try:
            from . import mask_tile_service
            mask_tile_service.clear_cache_for_project(project_id)
        except Exception:
            pass


# ---- Reads ------------------------------------------------------------------

def _row_to_project(row) -> dict:
    keys = row.keys()
    tile_px = int(row["tile_px"]) if "tile_px" in keys else 256
    mpp = float(row["meters_per_pixel"]) if "meters_per_pixel" in keys else 2.5
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "kind": row["kind"] if "kind" in keys else "raster",
        "mask_complete_required": bool(row["mask_complete_required"]),
        "tile_px": tile_px,
        "meters_per_pixel": mpp,
        "tile_meters": tile_px * mpp,
        "primary_mbtiles": row["primary_mbtiles"],
        "secondary_mbtiles": row["secondary_mbtiles"],
        "tertiary_mbtiles": row["tertiary_mbtiles"],
        "ref_mask_primary_mbtiles": row["ref_mask_primary_mbtiles"],
        "ref_mask_secondary_mbtiles": row["ref_mask_secondary_mbtiles"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


# Sanity bounds for tile_px. The lower bound is just "positive integer";
# the upper bound caps the per-tile mask body at ~16 MB (4096² bytes), past
# which browser memory and PNG encode times stop being interactive.
MIN_TILE_PX = 1
MAX_TILE_PX = 4096


def _validate_tile_geometry(tile_px: int, meters_per_pixel: float) -> None:
    if not isinstance(tile_px, int) or isinstance(tile_px, bool) \
            or tile_px < MIN_TILE_PX or tile_px > MAX_TILE_PX:
        raise HTTPException(400, detail={
            "error": "invalid_tile_px",
            "min": MIN_TILE_PX, "max": MAX_TILE_PX,
            "message": f"tile_px deve ser inteiro entre {MIN_TILE_PX} e {MAX_TILE_PX}",
        })
    if not isinstance(meters_per_pixel, (int, float)) \
            or isinstance(meters_per_pixel, bool) or meters_per_pixel <= 0:
        raise HTTPException(400, detail={
            "error": "invalid_meters_per_pixel",
            "message": "meters_per_pixel deve ser > 0",
        })


def get_project(project_id: int) -> dict | None:
    """Full project record + classes. Cached."""
    with _CACHE_LOCK:
        cached = _PROJECT_CACHE.get(project_id)
    if cached is not None:
        return cached
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            return None
        proj = _row_to_project(row)
        classes = conn.execute(
            "SELECT class_id AS id, name, color, ordering FROM project_classes "
            "WHERE project_id=? ORDER BY ordering, class_id",
            (project_id,),
        ).fetchall()
        proj["classes"] = [
            {"id": c["id"], "name": c["name"], "color": c["color"]}
            for c in classes
        ]
    finally:
        conn.close()
    with _CACHE_LOCK:
        _PROJECT_CACHE[project_id] = proj
    return proj


def list_projects_for_user(user_id: int, *, is_admin: bool = False) -> list[dict]:
    """Projects the user can see. Admins see everything (active flag still
    filters); non-admins see projects where they are a member."""
    conn = connect()
    try:
        if is_admin:
            rows = conn.execute(
                "SELECT * FROM projects WHERE active=1 ORDER BY id"
            ).fetchall()
            project_role = {r["id"]: "admin" for r in rows}
        else:
            rows = conn.execute(
                """SELECT p.*, m.role AS membership_role
                   FROM projects p
                   JOIN project_members m ON m.project_id=p.id
                   WHERE m.user_id=? AND p.active=1
                   ORDER BY p.id""",
                (user_id,),
            ).fetchall()
            project_role = {r["id"]: r["membership_role"] for r in rows}
    finally:
        conn.close()
    out = []
    for r in rows:
        d = _row_to_project(r)
        d["role"] = project_role.get(d["id"])
        out.append(d)
    return out


def has_any_tile(project_id: int) -> bool:
    """Cheap existence check used by the admin form to know whether tile
    geometry (tile_px / meters_per_pixel) is still editable. Not cached:
    the result flips the moment a tile is inserted, and tile inserts don't
    invalidate the project cache."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM tiles WHERE project_id=? LIMIT 1", (project_id,)
        ).fetchone()
    finally:
        conn.close()
    return bool(row)


def get_membership_role(project_id: int, user_id: int) -> str | None:
    """Returns the user's role inside the project, or None if not a member."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return row["role"] if row else None


def list_members(project_id: int) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT u.id, u.username, u.role AS global_role, u.active,
                      m.role AS project_role
               FROM project_members m
               JOIN users u ON u.id=m.user_id
               WHERE m.project_id=?
               ORDER BY u.username""",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---- Mutations --------------------------------------------------------------

_PROJECT_ROLES = ("operator", "reviewer")
# Supported annotation kinds. The DB CHECK on projects.kind still lists the
# retired 'vector'/'detection' kinds (legacy schema); creation is gated here.
PROJECT_KINDS = ("raster", "classification")
_ALLOWED_LAYER_FIELDS = {
    "primary_mbtiles", "secondary_mbtiles", "tertiary_mbtiles",
    "ref_mask_primary_mbtiles", "ref_mask_secondary_mbtiles",
}


_REQUIRED_URL_PLACEHOLDERS = ("{z}", "{x}", "{y}")


def _validate_layer_path(stored: str, *, required: bool) -> None:
    """Layer source must be a remote tile-server URL (with {z}/{x}/{y}
    placeholders) or a local mbtiles file that exists on disk. Empty/None
    only allowed for optional layers."""
    if stored is None or stored == "":
        if required:
            raise HTTPException(400, detail={
                "error": "missing_primary",
                "message": "primary_mbtiles é obrigatório",
            })
        return
    if is_remote_layer(stored):
        missing = [p for p in _REQUIRED_URL_PLACEHOLDERS if p not in stored]
        if missing:
            raise HTTPException(400, detail={
                "error": "missing_url_placeholders",
                "missing": missing,
                "message": f"URL precisa de {missing} no template",
            })
        return
    resolved = resolve_mbtiles_path(stored)
    if resolved is None or not resolved.exists():
        raise HTTPException(400, detail={
            "error": "mbtiles_not_found",
            "message": f"mbtiles não encontrado: {stored}",
            "path": str(resolved) if resolved else stored,
        })


def create_project(
    *, name: str, description: str = "",
    kind: str = "raster",
    mask_complete_required: bool = True,
    tile_px: int = 256, meters_per_pixel: float = 2.5,
    primary_mbtiles: str, secondary_mbtiles: str | None = None,
    tertiary_mbtiles: str | None = None,
    ref_mask_primary_mbtiles: str | None = None,
    ref_mask_secondary_mbtiles: str | None = None,
    classes: list[dict] | None = None,
    created_by: int,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, detail={"error": "invalid_name"})
    if kind not in PROJECT_KINDS:
        raise HTTPException(400, detail={"error": "invalid_kind"})
    _validate_tile_geometry(tile_px, meters_per_pixel)
    if not classes:
        raise HTTPException(400, detail={"error": "no_classes"})
    try:
        validate_classes(classes)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_classes",
                                          "message": str(e)})
    _validate_layer_path(primary_mbtiles, required=True)
    for opt in (secondary_mbtiles, tertiary_mbtiles,
                ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles):
        _validate_layer_path(opt, required=False)
    with transaction("IMMEDIATE") as conn:
        exists = conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone()
        if exists:
            raise HTTPException(409, detail={"error": "name_taken"})
        conn.execute(
            """INSERT INTO projects(name, description, kind,
               mask_complete_required, tile_px, meters_per_pixel,
               primary_mbtiles, secondary_mbtiles,
               tertiary_mbtiles, ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
               active, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (name, description, kind,
             1 if mask_complete_required else 0,
             int(tile_px), float(meters_per_pixel),
             primary_mbtiles, secondary_mbtiles or None, tertiary_mbtiles or None,
             ref_mask_primary_mbtiles or None, ref_mask_secondary_mbtiles or None,
             created_by, now_iso()),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for ord_idx, c in enumerate(classes):
            conn.execute(
                """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
                   VALUES(?,?,?,?,?)""",
                (pid, c["id"], c["name"], c["color"], ord_idx),
            )
        log_action(conn, created_by, None, "project_create", name)
    _invalidate(pid)
    return get_project(pid)


def clone_project(source_id: int, *, new_name: str | None, by_user: int) -> dict:
    """Duplicate a project's config (paths, classes, mask_complete_required)
    into a new project. Memberships are NOT copied — admins assign explicitly
    so cloning doesn't silently leak access. The clone starts active=True."""
    source = get_project(source_id)
    if not source:
        raise HTTPException(404, detail={"error": "project_not_found"})
    name = (new_name or "").strip() or f"{source['name']}_copia"
    with transaction("IMMEDIATE") as conn:
        if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, detail={"error": "name_taken"})
        conn.execute(
            """INSERT INTO projects(name, description, kind,
               mask_complete_required, tile_px, meters_per_pixel,
               primary_mbtiles, secondary_mbtiles,
               tertiary_mbtiles, ref_mask_primary_mbtiles, ref_mask_secondary_mbtiles,
               active, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (name, source["description"],
             source.get("kind", "raster"),
             1 if source["mask_complete_required"] else 0,
             int(source.get("tile_px", 256)),
             float(source.get("meters_per_pixel", 2.5)),
             source["primary_mbtiles"], source["secondary_mbtiles"],
             source["tertiary_mbtiles"], source["ref_mask_primary_mbtiles"],
             source["ref_mask_secondary_mbtiles"],
             by_user, now_iso()),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for ord_idx, c in enumerate(source.get("classes") or []):
            conn.execute(
                """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
                   VALUES(?,?,?,?,?)""",
                (new_id, c["id"], c["name"], c["color"], ord_idx),
            )
        log_action(conn, by_user, None, "project_clone", f"{source_id}->{new_id}")
    _invalidate(new_id)
    return get_project(new_id)


def update_project(project_id: int, *, fields: dict, updated_by: int) -> dict:
    """Patch a subset of fields. Validates layer paths if present.

    `kind` is intentionally NOT in the allow-list — flipping
    raster↔classification on an existing project would orphan every tile body. Admins must clone
    or recreate to switch kinds."""
    cols_allowed = {
        "name", "description", "mask_complete_required", "active",
        "tile_px", "meters_per_pixel",
        "primary_mbtiles", "secondary_mbtiles", "tertiary_mbtiles",
        "ref_mask_primary_mbtiles", "ref_mask_secondary_mbtiles",
    }
    bad = set(fields) - cols_allowed
    if bad:
        raise HTTPException(400, detail={"error": "unknown_fields", "fields": sorted(bad)})
    geometry_changing = "tile_px" in fields or "meters_per_pixel" in fields
    sets, params = [], []
    if "primary_mbtiles" in fields:
        _validate_layer_path(fields["primary_mbtiles"], required=True)
    for k in ("secondary_mbtiles", "tertiary_mbtiles",
              "ref_mask_primary_mbtiles", "ref_mask_secondary_mbtiles"):
        if k in fields:
            _validate_layer_path(fields[k], required=False)
    for k, v in fields.items():
        if k in ("mask_complete_required", "active"):
            v = 1 if v else 0
        if k == "tile_px":
            v = int(v)
        elif k == "meters_per_pixel":
            v = float(v)
        if k in _ALLOWED_LAYER_FIELDS and v == "":
            v = None
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return get_project(project_id)
    params.append(project_id)
    with transaction("IMMEDIATE") as conn:
        row = conn.execute(
            "SELECT id, tile_px, meters_per_pixel FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, detail={"error": "project_not_found"})
        if geometry_changing:
            new_px = int(fields.get("tile_px", row["tile_px"]))
            new_mpp = float(fields.get("meters_per_pixel", row["meters_per_pixel"]))
            _validate_tile_geometry(new_px, new_mpp)
            # Mask bytes encode tile_px²; bbox is baked at insert time.
            # Once any tile exists, both fields are immutable.
            locked = conn.execute(
                "SELECT 1 FROM tiles WHERE project_id=? LIMIT 1", (project_id,)
            ).fetchone()
            if locked:
                raise HTTPException(409, detail={
                    "error": "tile_geometry_locked",
                    "message": ("tile_px e meters_per_pixel só podem ser alterados "
                                "antes do projeto receber tiles."),
                })
        if "name" in fields:
            taken = conn.execute(
                "SELECT 1 FROM projects WHERE name=? AND id!=?",
                (fields["name"], project_id),
            ).fetchone()
            if taken:
                raise HTTPException(409, detail={"error": "name_taken"})
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params)
        log_action(conn, updated_by, None, "project_update", str(project_id))
    # Layer-source / meters_per_pixel / tile_px changes flip the raster pipeline
    # output for tiles already cached; drop the overlay cache so the next admin
    # map view re-renders with the new source.
    overlay_dirty = any(
        k in fields for k in (
            "primary_mbtiles", "secondary_mbtiles", "tertiary_mbtiles",
            "ref_mask_primary_mbtiles", "ref_mask_secondary_mbtiles",
            "tile_px", "meters_per_pixel",
        )
    )
    _invalidate(project_id, drop_overlay_cache=overlay_dirty)
    return get_project(project_id)


def set_classes(project_id: int, classes: list[dict], *, updated_by: int) -> dict:
    """Replace the project's class set. Refuses to delete a class that is in
    use by any tile (mask bytes are immutable; removing a class would orphan
    pixels). Adding new classes and renaming/recoloring existing ones is fine."""
    try:
        validate_classes(classes)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "invalid_classes", "message": str(e)})
    new_ids = {c["id"] for c in classes}
    with transaction("IMMEDIATE") as conn:
        existing = conn.execute(
            "SELECT class_id FROM project_classes WHERE project_id=?", (project_id,)
        ).fetchall()
        existing_ids = {r["class_id"] for r in existing}
        removed = existing_ids - new_ids
        if removed:
            # Class IDs are encoded into stored mask bytes; cannot drop while
            # tiles in this project still reference them.
            in_use = conn.execute(
                "SELECT COUNT(*) c FROM tiles WHERE project_id=?", (project_id,)
            ).fetchone()["c"]
            if in_use:
                raise HTTPException(409, detail={
                    "error": "class_in_use",
                    "removed": sorted(removed),
                    "message": "Não é possível remover classes em projeto com tiles existentes.",
                })
        conn.execute("DELETE FROM project_classes WHERE project_id=?", (project_id,))
        for ord_idx, c in enumerate(classes):
            conn.execute(
                """INSERT INTO project_classes(project_id, class_id, name, color, ordering)
                   VALUES(?,?,?,?,?)""",
                (project_id, c["id"], c["name"], c["color"], ord_idx),
            )
        log_action(conn, updated_by, None, "project_classes_update", str(project_id))
    # Palette changes (rename/recolor/add) propagate through the overlay LUT;
    # already-cached PNGs still hold the old colors until cleared.
    _invalidate(project_id, drop_overlay_cache=True)
    return get_project(project_id)


def add_member(project_id: int, user_id: int, role: str, *, by_user: int) -> None:
    if role not in _PROJECT_ROLES:
        raise HTTPException(400, detail={"error": "invalid_role"})
    with transaction("IMMEDIATE") as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise HTTPException(404, detail={"error": "project_not_found"})
        if not conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone():
            raise HTTPException(404, detail={"error": "user_not_found"})
        conn.execute(
            "INSERT OR REPLACE INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
            (project_id, user_id, role),
        )
        log_action(conn, by_user, None, "project_member_set", f"{project_id}:{user_id}:{role}")


def remove_member(project_id: int, user_id: int, *, by_user: int) -> None:
    with transaction("IMMEDIATE") as conn:
        conn.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        )
        log_action(conn, by_user, None, "project_member_remove", f"{project_id}:{user_id}")


def delete_project(project_id: int, *, by_user: int) -> None:
    """Hard-delete a project. Refuses (409 project_has_tiles) if the project
    has any tile — the tile bodies and history would otherwise dangle. Use
    `update_project(active=False)` to soft-disable instead."""
    with transaction("IMMEDIATE") as conn:
        if not conn.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone():
            raise HTTPException(404, detail={"error": "project_not_found"})
        tile_count = conn.execute(
            "SELECT COUNT(*) c FROM tiles WHERE project_id=?", (project_id,)
        ).fetchone()["c"]
        if tile_count:
            raise HTTPException(409, detail={
                "error": "project_has_tiles",
                "tile_count": tile_count,
                "message": "Não é possível deletar projeto com tiles. Desative-o (active=False) ou remova os tiles antes.",
            })
        log_action(conn, by_user, None, "project_delete", str(project_id))
        # ON DELETE CASCADE drops project_classes + project_members.
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    _invalidate(project_id)
    # Remove the per-project overlay cache file outright — keeping it would
    # leak disk space and create an orphan if a future project_id collides.
    try:
        from . import mask_tile_service
        mask_tile_service.delete_cache_for_project(project_id)
    except Exception:
        pass


# ---- FastAPI dependencies ---------------------------------------------------

def require_membership(
    project_id: int,
    user,
    *,
    min_role: str = "operator",
):
    """Return the user's project role, raising 403 if not a member or below
    the required role tier. Global admins always pass (they read/write any
    project to administer it — review eligibility for assignments is a
    separate check via tile_service._user_can_review_project)."""
    if user.role == "admin":
        return "admin"
    role = get_membership_role(project_id, user.id)
    if role is None:
        raise HTTPException(403, detail={"error": "not_project_member"})
    tiers = {"operator": 0, "reviewer": 1}
    if tiers.get(role, -1) < tiers.get(min_role, 0):
        raise HTTPException(403, detail={"error": "insufficient_project_role"})
    return role
