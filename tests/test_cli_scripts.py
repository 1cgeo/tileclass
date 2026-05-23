"""Coverage for the CLI scripts (agent/ops entry points) that had none:
create_admin (interactive), import_points (geodesic insert + dedup + block),
recolor_mbtiles (pure RGB remap), plus import smokes for the heavy build tools
so a syntax/import regression is caught."""
import io
import sys

import numpy as np
import pytest
from PIL import Image


def _seed_default(app_env):
    from backend.database import transaction
    from tests.conftest import _seed_test_project
    with transaction("IMMEDIATE") as conn:
        _seed_test_project(conn)


def _tile_count(project_name="default"):
    from backend.database import connect
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM tiles").fetchone()["c"]
    finally:
        conn.close()


def _run(module_main, argv):
    old = sys.argv
    sys.argv = argv
    try:
        module_main()
    finally:
        sys.argv = old


# ---- create_admin ----------------------------------------------------------

def test_create_admin_creates_admin_user(app_env, monkeypatch):
    from backend.scripts import create_admin
    monkeypatch.setattr("builtins.input", lambda *a: "novoadmin")
    monkeypatch.setattr(create_admin, "getpass", type("G", (), {"getpass": staticmethod(lambda *a: "secret123")}))
    create_admin.main()
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute("SELECT role, active FROM users WHERE username='novoadmin'").fetchone()
    finally:
        conn.close()
    assert row is not None and row["role"] == "admin" and row["active"] == 1


def test_create_admin_rejects_short_or_mismatched_password(app_env, monkeypatch):
    from backend.scripts import create_admin
    monkeypatch.setattr("builtins.input", lambda *a: "shortpw")
    # Two getpass calls return mismatched values → aborts without inserting.
    pws = iter(["abc", "different"])
    monkeypatch.setattr(create_admin, "getpass",
                        type("G", (), {"getpass": staticmethod(lambda *a: next(pws))}))
    create_admin.main()
    from backend.database import connect
    conn = connect()
    try:
        assert conn.execute("SELECT 1 FROM users WHERE username='shortpw'").fetchone() is None
    finally:
        conn.close()


# ---- import_points ---------------------------------------------------------

def test_import_points_inserts_dedups_and_blocks(app_env):
    _seed_default(app_env)
    from backend.scripts import import_points
    # One point → one tile.
    _run(import_points.main, ["import_points", "--point", "-23.55", "-46.63", "sp", "--project", "default"])
    assert _tile_count() == 1
    # Same point again → deduped (still one).
    _run(import_points.main, ["import_points", "--point", "-23.55", "-46.63", "sp", "--project", "default"])
    assert _tile_count() == 1
    # A 3×3 block around a far point → 9 new tiles.
    _run(import_points.main, ["import_points", "--point", "-10.0", "-40.0", "blk",
                              "--block", "3", "--project", "default"])
    assert _tile_count() == 1 + 9


def test_import_points_block_tiles_are_distinct_and_adjacent(app_env):
    _seed_default(app_env)
    from backend.scripts import import_points
    _run(import_points.main, ["import_points", "--point", "0.0", "0.0", "c",
                              "--block", "3", "--project", "default"])
    from backend.database import connect
    conn = connect()
    try:
        rows = conn.execute("SELECT bbox_west, bbox_east FROM tiles ORDER BY bbox_west").fetchall()
    finally:
        conn.close()
    assert len(rows) == 9
    wests = sorted({round(r["bbox_west"], 6) for r in rows})
    assert len(wests) == 3  # 3 distinct columns
    # Adjacent columns share an edge (east of one ≈ west of next, gap < ~1mm).
    easts = sorted({round(r["bbox_east"], 6) for r in rows})
    assert abs(easts[0] - wests[1]) < 1e-5


# ---- recolor_mbtiles (pure functions) --------------------------------------

def test_recolor_parse_pair():
    from backend.scripts.recolor_mbtiles import parse_pair
    assert parse_pair("ff0000:0000ff") == ((255, 0, 0), (0, 0, 255))
    assert parse_pair("#FF0000:#00FF00") == ((255, 0, 0), (0, 255, 0))
    with pytest.raises(ValueError):
        parse_pair("ff00:0000ff")  # not 3 bytes


def test_recolor_replaces_exact_rgb():
    from backend.scripts.recolor_mbtiles import recolor
    # A 4x4 PNG: half red, half green.
    arr = np.zeros((4, 4, 4), dtype=np.uint8)
    arr[..., 3] = 255
    arr[:2, :, 0] = 255          # top half red
    arr[2:, :, 1] = 255          # bottom half green
    buf = io.BytesIO(); Image.fromarray(arr, "RGBA").save(buf, format="PNG")
    mapping = [((255, 0, 0), (0, 0, 255))]  # red → blue
    _, _, _, out = recolor((10, 1, 2, buf.getvalue(), mapping))
    res = np.array(Image.open(io.BytesIO(out)).convert("RGBA"))
    assert (res[:2, :, 2] == 255).all() and (res[:2, :, 0] == 0).all()  # red→blue
    assert (res[2:, :, 1] == 255).all()                                 # green untouched


def test_recolor_no_match_passes_through_bytes():
    from backend.scripts.recolor_mbtiles import recolor
    arr = np.zeros((2, 2, 4), dtype=np.uint8); arr[..., 3] = 255; arr[..., 1] = 255  # all green
    buf = io.BytesIO(); Image.fromarray(arr, "RGBA").save(buf, format="PNG")
    data = buf.getvalue()
    _, _, _, out = recolor((0, 0, 0, data, [((255, 0, 0), (0, 0, 255))]))
    assert out is data  # unchanged bytes returned verbatim (no re-encode)


# ---- import smokes (heavy tools: just confirm they import cleanly) ---------

@pytest.mark.parametrize("mod", [
    "backend.scripts.build_mbtiles",
    "backend.scripts.recolor_mbtiles",
])
def test_heavy_script_imports(mod):
    import importlib
    m = importlib.import_module(mod)
    assert callable(m.main)
