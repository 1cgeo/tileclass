"""Backup + verify CLIs.

backup_db copies via sqlite3.backup() so it's WAL-consistent; verify_db
runs FK + schema + mask-integrity checks and exits non-zero on failure.
"""
import sqlite3
import sys
from datetime import datetime, timezone


def _run_cli(module_path: str, argv: list, monkeypatch=None) -> int:
    """Import a script's main() with custom argv and capture its exit code."""
    import importlib
    if module_path in sys.modules:
        del sys.modules[module_path]
    old_argv = sys.argv
    sys.argv = [module_path.split(".")[-1], *argv]
    try:
        mod = importlib.import_module(module_path)
        try:
            mod.main()
        except SystemExit as e:
            return int(e.code or 0)
    finally:
        sys.argv = old_argv
    return 0


# ---- backup ----------------------------------------------------------------

def test_backup_db_writes_consistent_copy(app_env, tmp_path):
    """The backup file is a standalone .db with the same row counts."""
    out = tmp_path / "out.db"
    code = _run_cli("backend.scripts.backup_db", [str(out)])
    assert code == 0
    assert out.exists() and out.stat().st_size > 0
    src = sqlite3.connect(app_env)
    dst = sqlite3.connect(out)
    try:
        s = src.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        d = dst.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert s == d
    finally:
        src.close(); dst.close()


def test_backup_refuses_to_overwrite(app_env, tmp_path):
    out = tmp_path / "x.db"
    out.write_bytes(b"")  # exists
    code = _run_cli("backend.scripts.backup_db", [str(out)])
    assert code != 0


def test_backup_auto_name_creates_timestamped_file(app_env, tmp_path):
    out_dir = tmp_path / "snapshots"
    code = _run_cli("backend.scripts.backup_db", [str(out_dir), "--auto-name"])
    assert code == 0
    files = list(out_dir.glob("tileclass-*.db"))
    assert len(files) == 1


# ---- verify ----------------------------------------------------------------

def _clear_seed_paths(app_env, tmp_path):
    """The seed project copies config.yaml's mbtiles paths verbatim, but
    those files aren't shipped in the repo. Replace primary with a stub
    that does exist and null the optional layers so verify_db has a
    fully-valid project to check against."""
    stub = tmp_path / "stub.mbtiles"
    stub.write_bytes(b"")
    conn = sqlite3.connect(app_env)
    try:
        conn.execute(
            """UPDATE projects SET primary_mbtiles=?,
               secondary_mbtiles=NULL, tertiary_mbtiles=NULL,
               ref_mask_primary_mbtiles=NULL, ref_mask_secondary_mbtiles=NULL""",
            (str(stub),),
        )
        conn.commit()
    finally:
        conn.close()


def test_verify_passes_on_clean_db(app_env, tmp_path):
    _clear_seed_paths(app_env, tmp_path)
    code = _run_cli("backend.scripts.verify_db", ["--quick"])
    assert code == 0


def test_verify_does_not_false_fail_on_non_raster_tiles(app_env, tmp_path):
    """verify_db's mask scan is raster-only (joins kind='raster'). A DB with
    valid vector/detection/classification tiles must still pass the full scan —
    their GeoJSON/class_id bodies are NOT mis-decoded as masks."""
    _clear_seed_paths(app_env, tmp_path)
    stub = str(tmp_path / "stub.mbtiles")
    import json
    fc = json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"class_id": 1},
         "geometry": {"type": "Polygon", "coordinates": [[[-50, -25], [-49.9, -25], [-49.9, -24.9], [-50, -24.9], [-50, -25]]]}}]})
    conn = sqlite3.connect(app_env)
    try:
        for pid, kind in ((2, "vector"), (3, "detection"), (4, "classification")):
            conn.execute("INSERT INTO projects(id,name,kind,tile_px,meters_per_pixel,"
                         "mask_complete_required,primary_mbtiles,active,created_at) "
                         "VALUES (?,?,?,256,2.5,1,?,1,'2026-01-01T00:00:00+00:00')",
                         (pid, f"k{pid}", kind, stub))
            conn.execute("INSERT INTO project_classes(project_id,class_id,name,color,ordering) "
                         "VALUES (?,1,'a','#112233',0)", (pid,))
        conn.execute("INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
                     "status,data_geojson,feature_count) VALUES (2,'v',-50,-25,-49.9,-24.9,'reviewed',?,1)", (fc,))
        conn.execute("INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
                     "status,data_geojson,feature_count) VALUES (3,'d',-50,-25,-49.9,-24.9,'reviewed',?,1)", (fc,))
        conn.execute("INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,"
                     "status,data_class_id) VALUES (4,'c',-50,-25,-49.9,-24.9,'reviewed',1)")
        conn.commit()
    finally:
        conn.close()
    code = _run_cli("backend.scripts.verify_db", [])  # full scan (not --quick)
    assert code == 0


def test_verify_catches_orphan_tile(app_env):
    """Insert a tile pointing at a non-existent project_id (bypass FK on
    pragma off) and verify_db should fail with code 2."""
    conn = sqlite3.connect(app_env)
    try:
        # Disable FK to inject the bad row, then re-enable for verify.
        conn.execute("PRAGMA foreign_keys=OFF")
        from backend.mask_utils import empty_mask_png
        conn.execute(
            """INSERT INTO tiles(project_id, name, bbox_west, bbox_south,
                                 bbox_east, bbox_north, status, data_png)
               VALUES (9999, 'orphan', 0, 0, 0.1, 0.1, 'pending', ?)""",
            (empty_mask_png(),),
        )
        conn.commit()
    finally:
        conn.close()
    code = _run_cli("backend.scripts.verify_db", ["--quick"])
    assert code == 2


def test_verify_catches_corrupt_mask(app_env, tmp_path, tiles):
    """A submitted tile whose mask contains a class id outside the
    project's palette is flagged."""
    _clear_seed_paths(app_env, tmp_path)
    conn = sqlite3.connect(app_env)
    try:
        from backend.mask_utils import encode_mask
        # Class 99 is not in the default project's 1..6 palette.
        bad = bytes([99]) * 65536
        conn.execute(
            "UPDATE tiles SET status='classified', data_png=? WHERE id=("
            "SELECT id FROM tiles ORDER BY id LIMIT 1)",
            (encode_mask(bad),),
        )
        conn.commit()
    finally:
        conn.close()
    code = _run_cli("backend.scripts.verify_db", [])
    assert code == 2


def test_verify_quick_skips_mask_scan(app_env, tmp_path, tiles):
    """--quick still catches schema issues but doesn't decode masks."""
    _clear_seed_paths(app_env, tmp_path)
    conn = sqlite3.connect(app_env)
    try:
        from backend.mask_utils import encode_mask
        bad = bytes([99]) * 65536
        conn.execute(
            "UPDATE tiles SET status='classified', data_png=? WHERE id=("
            "SELECT id FROM tiles ORDER BY id LIMIT 1)",
            (encode_mask(bad),),
        )
        conn.commit()
    finally:
        conn.close()
    # --quick skips mask decoding so this passes despite the bad mask.
    code = _run_cli("backend.scripts.verify_db", ["--quick"])
    assert code == 0
