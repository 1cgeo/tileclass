"""Shared helpers for tile-import / export CLIs.

Importing this module triggers no DB connection; functions take an open
sqlite3.Connection so callers control transaction boundaries."""
import sys


def resolve_project_arg(conn, project_arg: str | None, *,
                        allow_all: bool = False) -> int | None:
    """Resolve --project (id or name) to a project_id.

    - When `allow_all=True`, a missing arg returns None (callers treat as
      "every project"). This is the export_tiles flavour.
    - Otherwise a missing arg auto-resolves only when there is exactly one
      project; multiple projects raise a SystemExit so imports never go to
      the wrong project.
    """
    rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    if not rows:
        print("erro: nenhum projeto cadastrado. Crie um via UI admin ou "
              "/api/admin/projects antes de importar tiles.")
        sys.exit(1)
    if project_arg is None:
        if allow_all:
            return None
        if len(rows) == 1:
            return rows[0]["id"]
        names = ", ".join(f"{r['id']}={r['name']}" for r in rows)
        print(f"--project é obrigatório (vários projetos): {names}")
        sys.exit(1)
    try:
        pid = int(project_arg)
        if any(r["id"] == pid for r in rows):
            return pid
    except ValueError:
        pass
    for r in rows:
        if r["name"] == project_arg:
            return r["id"]
    print(f"projeto não encontrado: {project_arg}")
    sys.exit(1)


def insert_tile_dedup(conn, project_id: int, name: str,
                      bbox: tuple[float, float, float, float],
                      png: bytes) -> bool:
    """Insert a pending tile if no other tile in the same project covers the
    same bbox (~1 micrograu / 0.1m tolerance). Returns True on insert, False
    on skip. Distinct projects can cover the same bbox — uniqueness is per
    project so different themes over the same area don't collide."""
    west, south, east, north = bbox
    existing = conn.execute(
        """SELECT id FROM tiles
           WHERE project_id=?
             AND ABS(bbox_west  - ?) < 1e-6
             AND ABS(bbox_south - ?) < 1e-6
             AND ABS(bbox_east  - ?) < 1e-6
             AND ABS(bbox_north - ?) < 1e-6""",
        (project_id, west, south, east, north),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north,
                             status, data_png)
           VALUES (?,?,?,?,?,?,'pending',?)""",
        (project_id, name, west, south, east, north, png),
    )
    return True
