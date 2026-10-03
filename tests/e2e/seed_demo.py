"""Realistic demo dataset for screenshots and live presentations.

Builds a fresh DB plus a synthetic satellite-looking imagery .mbtiles so the
admin (dashboard, map, thumbnails, viewer) and the editor show plausible data
instead of empty canvases:

  * "Cobertura do solo — Demo" (raster, 6-class palette, 300 tiles) whose
    masks are sampled from the same procedural land-cover model that paints
    the imagery — painted regions line up with fields/rivers/town on screen.
  * "Uso do solo — Classificação" (classification, 80 tiles; class = the
    dominant land cover under the tile).
  * An inactive, empty pilot project (shows the "inativo" state).
  * 8 team members + admin, with ~30 days of action_log history (assign →
    classify/review cycles, pauses, problems) so durations, rate, ETA and the
    daily chart are meaningful.

Every generated file goes under <out_dir> (a temp dir owned by the caller).
Rendering the imagery takes ~1-2 min; set TILECLASS_DEMO_CACHE_DIR to reuse a
previous build (keyed by the rendering code).

Usage: python -m tests.e2e.seed_demo <db_path> <out_dir>

Logins: admin/admin123; team members (see DEMO_USERS) use "secret123".
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

# ---- Geography ---------------------------------------------------------------
# Cerrado farmland east of Brasília. Tiles are 256 px × 2.5 m = 640 m.
LAT0, LON0 = -15.55, -47.42
TILE_PX = 256
MPP = 2.5
TILE_M = TILE_PX * MPP
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))

RASTER_COLS, RASTER_ROWS = 20, 15
CLS_COLS, CLS_ROWS = 10, 8
CLS_OFFSET_COLS = RASTER_COLS + 1   # classification grid sits east, 1-tile gap

MIN_ZOOM, MAX_ZOOM = 10, 17

WATER, BUILT, FOREST, FIELD, CROP, SOIL = 1, 2, 3, 4, 5, 6
RASTER_CLASSES = [
    (WATER, "Massa d'água", "#377eb8"), (BUILT, "Área edificada", "#e41a1c"),
    (FOREST, "Floresta", "#4daf4a"), (FIELD, "Campo", "#ffff33"),
    (CROP, "Cultivo", "#984ea3"), (SOIL, "Terreno exposto", "#ff7f00"),
]
CLS_CLASSES = [
    (1, "Urbano", "#e41a1c"), (2, "Agrícola", "#984ea3"),
    (3, "Vegetação nativa", "#ff7f00"), (4, "Água", "#377eb8"),
    (5, "Solo exposto", "#a65628"),
]
# Land-cover model class -> classification-project class.
LANDCOVER_TO_CLS = {BUILT: 1, CROP: 2, FIELD: 3, FOREST: 3, WATER: 4, SOIL: 5}

PASSWORD = "secret123"
# (username, project role, speed factor — lower is faster)
DEMO_USERS = [
    ("ana.souza", "operator", 0.85), ("bruno.lima", "operator", 1.0),
    ("carla.mendes", "operator", 0.75), ("diego.rocha", "operator", 1.25),
    ("elisa.prado", "operator", 0.95), ("felipe.costa", "operator", 1.4),
    ("gabriela.reis", "reviewer", 0.9), ("henrique.alves", "reviewer", 1.1),
]

PROBLEM_NOTES = [
    "Nuvens cobrem quase metade do tile — impossível separar campo de cultivo.",
    "Sombra de nuvem sobre a mata ciliar; classe incerta na margem do rio.",
    "Imagem desfocada nesta região (mosaico com resolução menor).",
    "Tile parcialmente fora da área de interesse do projeto.",
    "Queimada recente: solo escuro parece água na composição RGB.",
]


# ---- Procedural land-cover model ---------------------------------------------
# Everything is a pure function of world meters (x east, y south of LAT0/LON0)
# so imagery at any zoom and the masks agree without storing a raster.

def _hash01(ix, iy, seed):
    h = (ix.astype(np.int64) * 374761393 + iy.astype(np.int64) * 668265263
         + seed * 1442695041) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h = h ^ (h >> 16)
    return (h & 0xFFFFFF).astype(np.float32) / float(0xFFFFFF)


def _vnoise(x, y, cell, seed):
    gx, gy = x / cell, y / cell
    ix, iy = np.floor(gx), np.floor(gy)
    fx, fy = gx - ix, gy - iy
    ux, uy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    ix, iy = ix.astype(np.int64), iy.astype(np.int64)
    a = _hash01(ix, iy, seed)
    b = _hash01(ix + 1, iy, seed)
    c = _hash01(ix, iy + 1, seed)
    d = _hash01(ix + 1, iy + 1, seed)
    return (a + (b - a) * ux) + ((c + (d - c) * ux) - (a + (b - a) * ux)) * uy


def _fbm(x, y, cells, seed):
    tot, amp, norm = 0.0, 1.0, 0.0
    for i, c in enumerate(cells):
        tot = tot + _vnoise(x, y, c, seed + i * 17) * amp
        norm += amp
        amp *= 0.5
    return tot / norm


# Fixed landmarks (world meters from the origin).
TOWN = (4200.0, 3300.0, 1500.0)        # center x, y, radius
LAKE = (9200.0, 4300.0, 800.0)


def _river_y(x):
    return 6100 + 700 * np.sin(x / 2300.0) + 260 * np.sin(x / 830.0 + 1.3)


PARCEL_W, PARCEL_H = 430.0, 340.0


def _parcels(x, y):
    """Warped field grid; some cells split in two. Returns (xw, yw, cx, cy,
    pid) where pid is an int64 parcel id (stable across zooms)."""
    wx = (_fbm(x, y, [900, 300], 11) - 0.5) * 140
    wy = (_fbm(x, y, [900, 300], 23) - 0.5) * 140
    xw, yw = x + wx, y + wy
    cx = np.floor(xw / PARCEL_W).astype(np.int64)
    cy = np.floor(yw / PARCEL_H).astype(np.int64)
    fx, fy = xw / PARCEL_W - cx, yw / PARCEL_H - cy
    split = _hash01(cx, cy, 13)
    cut = 0.35 + 0.3 * _hash01(cx, cy, 15)
    sub = np.where(split < 0.35, fx > cut, np.where(split < 0.65, fy > cut, False)).astype(np.int64)
    pid = (cx * 7919 + cy) * 2 + sub
    return xw, yw, cx, cy, pid


def landcover(x, y):
    """Class id per point (float arrays in meters)."""
    xw, yw, cx, cy, pid = _parcels(x, y)
    px, py = PARCEL_W, PARCEL_H
    r = _hash01(pid, pid * 0 + 3, 7)
    cls = np.where(r < 0.47, CROP, np.where(r < 0.73, FIELD,
                   np.where(r < 0.85, SOIL, FOREST))).astype(np.uint8)
    # Dirt roads along some parcel edges.
    fx, fy = (xw / px - cx) * px, (yw / py - cy) * py
    road = ((cx % 3 == 0) & (fx < 7)) | ((cy % 2 == 0) & (fy < 6))
    cls[road] = SOIL
    # Native vegetation remnants.
    cls[_fbm(x, y, [1600, 600, 180], 3) > 0.64] = FOREST
    # River with gallery forest of varying width.
    d = np.abs(y + (yw - y) * 0.3 - _river_y(x))
    gallery = 70 + 120 * _fbm(x, y, [700, 200], 5)
    cls[d < gallery] = FOREST
    cls[d < 16 + 8 * _vnoise(x, y, 300, 9)] = WATER
    # Reservoir.
    lx, ly, lr = LAKE
    dl = np.hypot((x - lx) * 0.8, y - ly) + (_fbm(x, y, [400, 120], 13) - 0.5) * 500
    cls[dl < lr + 60] = FIELD
    cls[dl < lr] = WATER
    # Town (overrides everything but water).
    tx, ty, tr = TOWN
    dt = np.hypot(x - tx, (y - ty) * 1.2) + (_fbm(x, y, [600, 200], 17) - 0.5) * 700
    cls[(dt < tr) & (cls != WATER)] = BUILT
    return cls


def _shade(cls, x, y, mpp):
    """RGB float image for class ids at world points; mpp = meters per pixel
    (fine texture fades out at coarse zooms instead of aliasing)."""
    h, w = cls.shape
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    fine = 1.0 if mpp < 4 else (0.5 if mpp < 12 else 0.2)
    lo = _fbm(x, y, [500, 150], 31)[..., None]
    hf = (_hash01(np.floor(x).astype(np.int64), np.floor(y).astype(np.int64), 41) - 0.5)[..., None]

    def put(mask, base, var_lo=18.0, var_hf=10.0):
        col = np.array(base, dtype=np.float32) + (lo - 0.5) * var_lo + hf * var_hf * fine
        rgb[mask] = col[mask]

    # Crops: per-parcel color + row stripes with a per-parcel heading.
    _, _, _, _, pid = _parcels(x, y)
    cx, cy = pid, pid * 0 + 1
    pr = _hash01(cx, cy, 99)[..., None]
    palette = np.array([[86, 122, 52], [112, 142, 62], [150, 116, 84], [184, 166, 104],
                        [96, 132, 70], [132, 100, 70]], dtype=np.float32)
    pcol = palette[np.minimum((pr[..., 0] * len(palette)).astype(int), len(palette) - 1)]
    ang = _hash01(cx, cy, 5)[..., None] * math.pi
    stripes = np.sin((x[..., None] * np.cos(ang) + y[..., None] * np.sin(ang)) / 1.6) * 7 * fine
    m = cls == CROP
    rgb[m] = (pcol + (lo - 0.5) * 22 + stripes + hf * 6 * fine)[m]

    put(cls == FIELD, (150, 150, 98), 34, 12)
    crowns = (_vnoise(x, y, 4.5, 51) - 0.5)[..., None] * 34 * fine
    m = cls == FOREST
    rgb[m] = (np.array([42, 74, 44], np.float32) + (lo - 0.5) * 16 + crowns + hf * 6 * fine)[m]
    put(cls == WATER, (34, 62, 76), 10, 3)
    put(cls == SOIL, (176, 124, 88), 20, 14)

    # Town: blocks with streets, lots with a house + yard, scattered trees.
    m = cls == BUILT
    if m.any():
        bx, by = np.mod(x, 112.0), np.mod(y, 92.0)
        street = (bx < 12) | (by < 12)
        u, v = bx - 12, by - 12                       # block-local (0..100, 0..80)
        lot_c = np.floor(u / 14.3).astype(np.int64)
        lot_r = (v >= 40).astype(np.int64)
        bid = np.floor(x / 112.0).astype(np.int64) * 977 + np.floor(y / 92.0).astype(np.int64)
        lid = bid * 64 + lot_c * 2 + lot_r
        lu, lv = np.mod(u, 14.3), np.where(lot_r == 1, 80 - v, v)   # lv: distance from street
        depth = 9 + 13 * _hash01(lid, lid * 0, 63)
        vacant = _hash01(lid, lid * 0, 67) < 0.12
        house = (lu > 1.6) & (lu < 12.6) & (lv > 3) & (lv < 3 + depth) & ~vacant
        roofs = np.array([[178, 94, 72], [206, 204, 198], [130, 130, 136],
                          [170, 154, 134], [192, 122, 94], [150, 82, 66]], dtype=np.float32)
        lot = _hash01(lid, lid * 0, 61)
        rcol = roofs[np.minimum((lot * len(roofs)).astype(int), len(roofs) - 1)]
        yard = np.where((_hash01(lid, lid * 0, 69) < 0.5)[..., None],
                        np.array([118, 128, 86], np.float32), np.array([152, 140, 116], np.float32))
        tree = (_vnoise(x, y, 7, 71) > 0.8) & ~house
        town = np.where(street[..., None], np.array([104, 104, 110], np.float32),
               np.where(house[..., None], rcol, yard))
        town = np.where(tree[..., None] & ~street[..., None], np.array([58, 88, 54], np.float32), town)
        # Coarse zooms: fade the street grid (it would alias into moiré).
        ft = float(np.clip((4.0 - mpp) / 2.0, 0.0, 1.0))
        flat = np.array([146, 138, 126], np.float32) + (lo - 0.5) * 30
        town = town * ft + flat * (1 - ft)
        rgb[m] = (town + hf * 8 * ft)[m]

    # Mild haze + contrast for a "satellite" look.
    rgb = rgb * 0.94 + 10
    return np.clip(rgb, 0, 255).astype(np.uint8)


# ---- Coordinate helpers ------------------------------------------------------

def to_meters(lon, lat):
    return (lon - LON0) * M_PER_DEG_LON, (LAT0 - lat) * M_PER_DEG_LAT


def tile_bbox(col, row):
    """(west, south, east, north) of grid cell (col, row); rows grow south.
    Centers walk whole tile widths geodesically (backend.geo), so neighbours
    share edges exactly like `import_points --block`."""
    from backend.geo import bbox_from_center, offset_center
    lat, lon = offset_center(LAT0, LON0, col, -row, TILE_M)
    return bbox_from_center(lat, lon, TILE_M)


def mask_for_bbox(bbox, jitter_seed=None):
    w, s, e, n = bbox
    t = (np.arange(TILE_PX, dtype=np.float64) + 0.5) / TILE_PX
    lon = w + (e - w) * t[None, :]
    lat = n - (n - s) * t[:, None]
    x, y = to_meters(lon, lat)
    x, y = np.broadcast_to(x, (TILE_PX, TILE_PX)), np.broadcast_to(y, (TILE_PX, TILE_PX))
    if jitter_seed is not None:  # operators don't trace edges perfectly
        x = x + (_vnoise(x, y, 14, jitter_seed) - 0.5) * 7
        y = y + (_vnoise(x, y, 14, jitter_seed + 1) - 0.5) * 7
    return landcover(x, y)


# ---- Imagery pyramid ---------------------------------------------------------

def _render_xyz(args):
    z, tx, ty = args
    n = 1 << z
    i = (np.arange(256, dtype=np.float64) + 0.5) / 256
    lon = (tx + i)[None, :] / n * 360.0 - 180.0
    yy = (ty + i)[:, None] / n
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * yy))))
    x, y = to_meters(lon, lat)
    x, y = np.broadcast_to(x, (256, 256)).copy(), np.broadcast_to(y, (256, 256)).copy()
    mpp = 156543.03 * math.cos(math.radians(LAT0)) / n
    jx = (_vnoise(x, y, 6, 81) - 0.5) * 5
    jy = (_vnoise(x, y, 6, 83) - 0.5) * 5
    cls = landcover(x + jx, y + jy)
    rgb = _shade(cls, x, y, mpp)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=84)
    return z, tx, ty, buf.getvalue()


def _lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def build_imagery(path, bounds):
    w, s, e, n = bounds
    jobs = []
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        pad = 0.004 if z >= 16 else 0.03   # degrees of margin around the grids
        x0, y0 = _lonlat_to_tile(w - pad, n + pad, z)
        x1, y1 = _lonlat_to_tile(e + pad, s - pad, z)
        for tx in range(int(x0), int(x1) + 1):
            for ty in range(int(y0), int(y1) + 1):
                jobs.append((z, tx, ty))
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE metadata (name TEXT, value TEXT);"
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);"
        "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);"
    )
    db.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("name", "TileClass demo imagery"), ("format", "jpg"), ("type", "baselayer"),
        ("minzoom", str(MIN_ZOOM)), ("maxzoom", str(MAX_ZOOM)),
        ("bounds", f"{w},{s},{e},{n}"), ("description", "Synthetic imagery (demo)"),
    ])
    workers = max(1, min(16, (os.cpu_count() or 2) - 1))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for z, tx, ty, data in pool.map(_render_xyz, jobs, chunksize=16):
            db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, tx, (1 << z) - 1 - ty, data))
    db.commit()
    db.close()
    return len(jobs)


def _imagery_cached(path, bounds):
    """Build the imagery, or copy it from $TILECLASS_DEMO_CACHE_DIR when a
    build with identical rendering code already exists there (rendering takes
    ~1-2 min; handy when iterating on screenshots)."""
    cache_dir = os.environ.get("TILECLASS_DEMO_CACHE_DIR")
    if not cache_dir:
        return build_imagery(path, bounds)
    import hashlib
    import inspect
    import shutil
    src = "".join(inspect.getsource(f) for f in (
        _hash01, _vnoise, _fbm, _parcels, _river_y, landcover, _shade, _render_xyz, build_imagery))
    key = hashlib.sha1(f"{src}{bounds}{LAKE}{TOWN}".encode()).hexdigest()[:12]
    cached = os.path.join(cache_dir, f"demo_imagery_{key}.mbtiles")
    if not os.path.exists(cached):
        os.makedirs(cache_dir, exist_ok=True)
        build_imagery(cached + ".tmp", bounds)
        os.replace(cached + ".tmp", cached)
    shutil.copyfile(cached, path)
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    finally:
        db.close()


# ---- History simulation ------------------------------------------------------

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat()


def _work_instants(rng, count, days=30):
    """`count` timestamps over the last `days`, ramping up (team onboarding)
    with weekend dips, during Brasília business hours (UTC-3)."""
    weights = []
    for d in range(days):
        day = (NOW - timedelta(days=days - 1 - d)).date()
        ramp = 0.45 + 0.85 * (d / (days - 1))
        weekend = 0.2 if day.weekday() >= 5 else 1.0
        weights.append(ramp * weekend * rng.uniform(0.8, 1.2))
    out = []
    for _ in range(count):
        d = rng.choices(range(days), weights=weights)[0]
        day = NOW - timedelta(days=days - 1 - d)
        start = day.replace(hour=11, minute=30, second=0)   # 08:30 BRT
        t = start + timedelta(seconds=rng.uniform(0, 9.5 * 3600))
        if t > NOW - timedelta(minutes=20):
            t = NOW - timedelta(minutes=rng.uniform(20, 300))
        out.append(t)
    return sorted(out)


_note_counter = iter(range(10**6))


def _next_note():
    """Cycle through PROBLEM_NOTES so the Problemas screen shows variety."""
    return next(_note_counter) % len(PROBLEM_NOTES)


class Log:
    def __init__(self):
        self.rows = []

    def add(self, uid, tid, action, at, detail=None):
        self.rows.append((uid, tid, action, detail, iso(at)))


def _cycle(rng, log, uid, tid, done_at, median_s, speed, assign_action, done_action):
    dur = rng.lognormvariate(math.log(median_s * speed), 0.35)
    pause = 0.0
    if rng.random() < 0.12:
        pause = rng.uniform(300, 1500)
    start = done_at - timedelta(seconds=dur + pause)
    log.add(uid, tid, assign_action, start)
    if pause:
        p_at = start + timedelta(seconds=dur * rng.uniform(0.2, 0.7))
        log.add(uid, tid, "pause", p_at)
        log.add(uid, tid, "resume", p_at + timedelta(seconds=pause))
    log.add(uid, tid, done_action, done_at)
    return start


def _plan_statuses(rng, n, counts):
    """Spatial sweep: earliest tiles in grid order are done, frontier is in
    progress, the rest pending; problems/blocked are sprinkled in."""
    order = list(range(n))
    # Light shuffle within windows so the frontier isn't a perfect line.
    for i in range(0, n, 6):
        win = order[i:i + 6]
        rng.shuffle(win)
        order[i:i + 6] = win
    plan = {}
    cursor = 0
    for status in ("reviewed", "in_review", "classified", "in_progress"):
        for _ in range(counts[status]):
            plan[order[cursor]] = status
            cursor += 1
    for i in order[cursor:]:
        plan[i] = "pending"
    done = [i for i in order[:counts["reviewed"] + counts["classified"]]]
    pend = order[cursor:]
    for i in rng.sample(pend, counts["problem"]):
        plan[i] = "problem"
    blocked_pending = rng.sample([i for i in pend if plan[i] == "pending"], counts["blocked_pending"])
    for i in blocked_pending:
        plan[i] = "blocked:pending"
    for i in rng.sample([i for i in done if plan[i] == "classified"], counts["blocked_classified"]):
        plan[i] = "blocked:classified"
    return plan


def seed_project(conn, rng, log, *, pid, kind, cols, rows, col_offset, prefix, counts,
                 operators, reviewers, speeds, classify_median, review_median):
    from backend.mask_utils import encode_mask, empty_mask_png
    empty = empty_mask_png(TILE_PX)
    n = cols * rows
    plan = _plan_statuses(rng, n, counts)
    # Classification order: reviewed tiles first (grid order), then the review
    # backlog — tiles awaiting review are the most recently classified ones.
    rank = {"reviewed": 0, "blocked:classified": 1, "in_review": 2, "classified": 3}
    done_idx = sorted((i for i, s in plan.items() if s in rank), key=lambda i: (rank[plan[i]], i))
    instants = _work_instants(rng, len(done_idx))
    classify_at = dict(zip(done_idx, instants))
    op_weights = [1.0 / speeds[u] for u in operators]

    for i in range(n):
        r, c = divmod(i, cols)
        bbox = tile_bbox(c + col_offset, r)
        name = f"{prefix}-r{r:02d}c{c:02d}"
        status = plan[i]
        blocked_from = None
        if status.startswith("blocked:"):
            blocked_from = status.split(":")[1]
            status = "blocked"
        cols_ = dict(project_id=pid, name=name, bbox_west=bbox[0], bbox_south=bbox[1],
                     bbox_east=bbox[2], bbox_north=bbox[3], status=status,
                     blocked_from=blocked_from)
        effective = blocked_from or status
        tid = conn.execute(
            "INSERT INTO tiles(project_id, name, bbox_west, bbox_south, bbox_east, bbox_north, "
            "status, blocked_from) VALUES (:project_id,:name,:bbox_west,:bbox_south,:bbox_east,"
            ":bbox_north,:status,:blocked_from)", cols_).lastrowid

        lc = mask_for_bbox(bbox, jitter_seed=200 + i) if effective != "pending" else None
        upd = {}
        if effective in ("reviewed", "in_review", "classified"):
            op = rng.choices(operators, weights=op_weights)[0]
            uid = op
            t_cls = classify_at[i]
            _cycle(rng, log, uid, tid, t_cls, classify_median, speeds[uid], "assign_classify", "classify")
            upd.update(classified_by=uid, classified_at=iso(t_cls))
            if kind == "raster":
                counts_px = np.bincount(lc.ravel(), minlength=256)
                upd["class_counts"] = json.dumps({str(k): int(v) for k, v in enumerate(counts_px) if v})
                upd["data_png"] = encode_mask(lc.tobytes(), TILE_PX)
            else:
                vals, cnt = np.unique(lc, return_counts=True)
                upd["data_class_id"] = LANDCOVER_TO_CLS[int(vals[np.argmax(cnt)])]
            eligible = [u for u in reviewers if u != uid]
            rev = rng.choice(eligible)
            if effective == "reviewed":
                lag = timedelta(hours=rng.uniform(3, 30))
                t_rev = min(t_cls + lag, NOW - timedelta(minutes=rng.uniform(5, 90)))
                if t_rev <= t_cls:
                    t_rev = t_cls + timedelta(minutes=10)
                _cycle(rng, log, rev, tid, t_rev, review_median, speeds[rev], "assign_review", "review")
                upd.update(reviewed_by=rev, reviewed_at=iso(t_rev))
            elif effective == "in_review":
                t_a = NOW - timedelta(minutes=rng.uniform(3, 50))
                log.add(rev, tid, "assign_review", t_a)
                upd.update(assigned_to=rev)
        elif effective == "in_progress":
            uid = rng.choice(operators)
            t_a = NOW - timedelta(minutes=rng.uniform(4, 180))
            log.add(uid, tid, "assign_classify", t_a)
            # No heartbeat on purpose: the 5-min stale sweep would auto-pause
            # these as soon as someone calls /next during a demo.
            upd.update(assigned_to=uid)
            if kind == "raster":
                partial = lc.copy()
                cut = int(TILE_PX * rng.uniform(0.25, 0.8))
                partial[cut:, :] = 255
                upd["data_png"] = encode_mask(partial.tobytes(), TILE_PX)
            if rng.random() < 0.35:
                t_p = t_a + timedelta(minutes=rng.uniform(2, 20))
                log.add(uid, tid, "pause", t_p)
                upd["paused_at"] = iso(t_p)
        elif effective == "problem":
            uid = rng.choice(operators)
            t_r = NOW - timedelta(days=rng.uniform(0.2, 9))
            log.add(uid, tid, "assign_classify", t_r - timedelta(minutes=rng.uniform(1, 6)))
            note = PROBLEM_NOTES[_next_note()]
            log.add(uid, tid, "report_problem", t_r, note)
            upd["problem_note"] = note
        if kind == "raster" and "data_png" not in upd and effective != "problem":
            upd["data_png"] = empty
        if status == "blocked":
            log.add(1, tid, "block", NOW - timedelta(days=rng.uniform(0.5, 6)),
                    "Aguardando nova imagem sem nuvens")
        if upd:
            sets = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE tiles SET {sets} WHERE id=?", [*upd.values(), tid])


def main(db_path: str, out_dir: str) -> None:
    import backend.config as cfgmod
    import backend.database as dbmod
    import backend.auth as authmod

    original = cfgmod.get_config

    def patched():
        return {**original(), "database": {"path": db_path}}
    cfgmod.get_config = patched
    dbmod.get_config = patched
    authmod.get_config = patched
    dbmod._DB_PATH = None
    from backend.database import connect, init_db
    from backend.auth import hash_password

    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(20261003)

    # Imagery covers both grids.
    corners = [tile_bbox(0, 0), tile_bbox(CLS_OFFSET_COLS + CLS_COLS - 1, RASTER_ROWS - 1)]
    bounds = (min(c[0] for c in corners), min(c[1] for c in corners),
              max(c[2] for c in corners), max(c[3] for c in corners))
    imagery = os.path.abspath(os.path.join(out_dir, "demo_imagery.mbtiles"))
    ntiles = _imagery_cached(imagery, bounds)

    init_db()
    conn = connect()
    created = NOW - timedelta(days=45)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
            "VALUES ('admin',?,'admin',1,1,?)", (hash_password("admin123"), iso(created)))
        pw = hash_password(PASSWORD)  # one hash for all demo users (bcrypt is slow)
        uids, speeds = {}, {}
        for k, (uname, role, speed) in enumerate(DEMO_USERS):
            uid = conn.execute(
                "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
                "VALUES (?,?,'operator',1,?,?)",
                (uname, pw, 1 if role == "reviewer" else 0,
                 iso(created + timedelta(days=k * 1.5, hours=k)))).lastrowid
            uids[uname] = (uid, role)
            speeds[uid] = speed
        # A former team member (inactive) for the users screen.
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, can_review, created_at) "
            "VALUES ('marcos.vieira',?,'operator',0,0,?)", (pw, iso(created - timedelta(days=20))))
        operators = [u for u, r in uids.values() if r == "operator"]
        reviewers = [u for u, r in uids.values() if r == "reviewer"]
        # The fastest operator also reviews (exercises reviewer ≠ classifier).
        senior = uids["carla.mendes"][0]

        def add_project(name, desc, kind, classes, active=1, days_ago=40):
            pid = conn.execute(
                "INSERT INTO projects(name, description, kind, tile_px, meters_per_pixel, "
                "mask_complete_required, primary_mbtiles, active, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?)",
                (name, desc, kind, TILE_PX, MPP, 1 if kind == "raster" else 0, imagery, active,
                 iso(NOW - timedelta(days=days_ago)))).lastrowid
            for o, (cid, cname, color) in enumerate(classes):
                conn.execute("INSERT INTO project_classes(project_id, class_id, name, color, ordering) "
                             "VALUES (?,?,?,?,?)", (pid, cid, cname, color, o))
            conn.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?,1,'admin')", (pid,))
            return pid

        log = Log()
        p1 = add_project("Cobertura do solo — Demo",
                         "Segmentação de cobertura do solo no entorno de Formosa (GO), 2,5 m/px.",
                         "raster", RASTER_CLASSES)
        for uid, role in uids.values():
            r = "reviewer" if (role == "reviewer" or uid == senior) else "operator"
            conn.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
                         (p1, uid, r))
        seed_project(conn, rng, log, pid=p1, kind="raster", cols=RASTER_COLS, rows=RASTER_ROWS,
                     col_offset=0, prefix="FSA",
                     counts=dict(reviewed=158, in_review=5, classified=16, in_progress=9,
                                 problem=4, blocked_pending=2, blocked_classified=1),
                     operators=operators, reviewers=reviewers + [senior], speeds=speeds,
                     classify_median=250, review_median=80)

        p2 = add_project("Uso do solo — Classificação",
                         "Classe dominante por tile para triagem rápida de áreas.",
                         "classification", CLS_CLASSES, days_ago=25)
        team2 = ["ana.souza", "bruno.lima", "elisa.prado", "gabriela.reis"]
        for uname in team2:
            uid, role = uids[uname]
            conn.execute("INSERT INTO project_members(project_id, user_id, role) VALUES (?,?,?)",
                         (p2, uid, "reviewer" if role == "reviewer" else "operator"))
        seed_project(conn, rng, log, pid=p2, kind="classification", cols=CLS_COLS, rows=CLS_ROWS,
                     col_offset=CLS_OFFSET_COLS, prefix="URB",
                     counts=dict(reviewed=38, in_review=2, classified=12, in_progress=2,
                                 problem=2, blocked_pending=1, blocked_classified=0),
                     operators=[uids[u][0] for u in team2[:3]],
                     reviewers=[uids["gabriela.reis"][0], senior], speeds=speeds,
                     classify_median=28, review_median=12)

        add_project("Piloto Sentinel-2 (arquivado)", "Teste inicial com imagens de 10 m — encerrado.",
                    "raster", RASTER_CLASSES, active=0, days_ago=60)

        log.rows.sort(key=lambda r: r[4])
        conn.executemany("INSERT INTO action_log(user_id, tile_id, action, detail, created_at) "
                         "VALUES (?,?,?,?,?)", log.rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"seeded demo: {len(log.rows)} log rows, {ntiles} imagery tiles -> {imagery}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
