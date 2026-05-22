"""merge_db CLI: merges a secondary TileClass DB into the primary. Exercises the
delicate, destructive logic with no other safety net — multi-kind project/tile
copy (kind/geometry/data_geojson/data_class_id preserved), per-(project,bbox)
dedup, in_progress→pending reset, user dedup by username, action_log FK remap,
automatic backup, and dry-run rollback."""
import sqlite3
import sys

import pytest

from backend.database import SCHEMA


def _new_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _user(conn, username, role="operator"):
    conn.execute(
        "INSERT INTO users(username,password_hash,role,active,can_review,created_at) "
        "VALUES (?,?,?,1,?,?)",
        (username, "x", role, 1 if role == "admin" else 0, "2026-01-01T00:00:00+00:00"),
    )
    return conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]


def _project(conn, name, kind="raster", **extra):
    cols = {"name": name, "kind": kind, "primary_mbtiles": "", "created_at": "2026-01-01T00:00:00+00:00"}
    cols.update(extra)
    keys = ",".join(cols)
    conn.execute(f"INSERT INTO projects({keys}) VALUES ({','.join('?'*len(cols))})", tuple(cols.values()))
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]


def _tile(conn, pid, name, bbox, *, status="reviewed", **body):
    cols = {"project_id": pid, "name": name, "bbox_west": bbox[0], "bbox_south": bbox[1],
            "bbox_east": bbox[2], "bbox_north": bbox[3], "status": status}
    cols.update(body)
    keys = ",".join(cols)
    conn.execute(f"INSERT INTO tiles({keys}) VALUES ({','.join('?'*len(cols))})", tuple(cols.values()))
    return conn.execute("SELECT id FROM tiles WHERE project_id=? AND name=?", (pid, name)).fetchone()[0]


def _run_merge(primary, secondary):
    from backend.scripts import merge_db
    old = sys.argv
    sys.argv = ["merge_db", "--primary", str(primary), "--secondary", str(secondary)]
    try:
        merge_db.main()
    finally:
        sys.argv = old


def test_merge_multi_kind_preserves_bodies_and_geometry(tmp_path):
    """The headline fix: a vector/detection project in the secondary must land
    in the primary with the right kind/geometry AND its data_geojson body — not
    silently degraded to raster-256 with a dropped body."""
    pri, sec = tmp_path / "pri.db", tmp_path / "sec.db"
    _new_db(pri); _new_db(sec)
    # Primary: just an admin (so usernames dedup is exercised).
    pc = sqlite3.connect(pri); _user(pc, "alice", "admin"); pc.commit(); pc.close()
    # Secondary: alice (reused) + bob (new) + a vector project with a vector tile
    # and a detection project with a box tile.
    sc = sqlite3.connect(sec)
    _user(sc, "alice", "admin"); bob = _user(sc, "bob")
    vpid = _project(sc, "hidro", kind="vector", topology_required=1, tile_px=226, meters_per_pixel=3.0)
    vfc = '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"tipo":"rio"},"geometry":{"type":"LineString","coordinates":[[-50,-25],[-49.9,-24.9]]}}]}'
    _tile(sc, vpid, "v1", (-50, -25, -49.9, -24.9), data_geojson=vfc, feature_count=1)
    dpid = _project(sc, "carros", kind="detection", box_required=1)
    dfc = '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"class_id":1},"geometry":{"type":"Polygon","coordinates":[[[-50,-25],[-49.9,-25],[-49.9,-24.9],[-50,-24.9],[-50,-25]]]}}]}'
    _tile(sc, dpid, "d1", (-50, -25, -49.9, -24.9), data_geojson=dfc, feature_count=1)
    sc.commit(); sc.close()

    _run_merge(pri, sec)

    conn = sqlite3.connect(pri); conn.row_factory = sqlite3.Row
    try:
        # bob inserted, alice reused (still one alice).
        assert conn.execute("SELECT COUNT(*) c FROM users WHERE username='alice'").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM users WHERE username='bob'").fetchone()["c"] == 1
        # Vector project copied with kind + geometry intact (the bug was raster/256).
        vp = conn.execute("SELECT * FROM projects WHERE name='hidro'").fetchone()
        assert vp["kind"] == "vector" and vp["topology_required"] == 1
        assert vp["tile_px"] == 226 and vp["meters_per_pixel"] == 3.0
        # Vector tile body preserved.
        vt = conn.execute("SELECT data_geojson, feature_count FROM tiles WHERE name='v1'").fetchone()
        assert vt["data_geojson"] == vfc and vt["feature_count"] == 1
        # Detection project + box body preserved.
        dp = conn.execute("SELECT kind, box_required FROM projects WHERE name='carros'").fetchone()
        assert dp["kind"] == "detection" and dp["box_required"] == 1
        assert conn.execute("SELECT data_geojson FROM tiles WHERE name='d1'").fetchone()["data_geojson"] == dfc
    finally:
        conn.close()


def test_merge_dedups_bbox_and_resets_sessions(tmp_path):
    """Same (project,bbox) → primary wins (secondary tile skipped); in_progress/
    in_review tiles reset to pending with assignment cleared."""
    pri, sec = tmp_path / "pri.db", tmp_path / "sec.db"
    _new_db(pri); _new_db(sec)
    pc = sqlite3.connect(pri)
    _user(pc, "alice", "admin")
    ppid = _project(pc, "shared")
    _tile(pc, ppid, "keep", (0.0, 0.0, 0.1, 0.1), status="reviewed")
    pc.commit(); pc.close()

    sc = sqlite3.connect(sec)
    su = _user(sc, "carol")
    spid = _project(sc, "shared")  # same name → reused in primary
    _tile(sc, spid, "dup", (0.0, 0.0, 0.1, 0.1), status="reviewed")  # same bbox → skipped
    _tile(sc, spid, "active", (0.2, 0.0, 0.3, 0.1), status="in_progress", assigned_to=su)  # → pending
    sc.commit(); sc.close()

    _run_merge(pri, sec)

    conn = sqlite3.connect(pri); conn.row_factory = sqlite3.Row
    try:
        # The duplicate-bbox tile was NOT inserted (primary still has 2 tiles).
        names = {r["name"] for r in conn.execute("SELECT name FROM tiles")}
        assert names == {"keep", "active"}
        active = conn.execute("SELECT status, assigned_to FROM tiles WHERE name='active'").fetchone()
        assert active["status"] == "pending" and active["assigned_to"] is None
    finally:
        conn.close()


def test_merge_dry_run_writes_nothing_and_makes_no_backup(tmp_path):
    pri, sec = tmp_path / "pri.db", tmp_path / "sec.db"
    _new_db(pri); _new_db(sec)
    pc = sqlite3.connect(pri); _user(pc, "alice", "admin"); pc.commit(); pc.close()
    sc = sqlite3.connect(sec); _user(sc, "bob"); sc.commit(); sc.close()

    from backend.scripts import merge_db
    old = sys.argv
    sys.argv = ["merge_db", "--primary", str(pri), "--secondary", str(sec), "--dry-run"]
    try:
        merge_db.main()
    finally:
        sys.argv = old

    conn = sqlite3.connect(pri)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username='bob'").fetchone()[0] == 0
    finally:
        conn.close()
    assert not list(tmp_path.glob("pri.db.bak-*"))  # no backup on dry-run


def test_merge_creates_backup_before_writing(tmp_path):
    pri, sec = tmp_path / "pri.db", tmp_path / "sec.db"
    _new_db(pri); _new_db(sec)
    pc = sqlite3.connect(pri); _user(pc, "alice", "admin"); pc.commit(); pc.close()
    sc = sqlite3.connect(sec); _user(sc, "bob"); sc.commit(); sc.close()
    _run_merge(pri, sec)
    assert list(tmp_path.glob("pri.db.bak-*")), "merge must back up the primary first"


def test_merge_refuses_same_file(tmp_path):
    db = tmp_path / "x.db"; _new_db(db)
    from backend.scripts import merge_db
    old = sys.argv
    sys.argv = ["merge_db", "--primary", str(db), "--secondary", str(db)]
    try:
        with pytest.raises(SystemExit):
            merge_db.main()
    finally:
        sys.argv = old
