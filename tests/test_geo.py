"""Invariante crítica do projeto: cada tile é sempre 256×256 pixels a
2.5 m/pixel, qualquer latitude. A bbox é computada por geodésica WGS84
a partir do centro, então o tamanho em metros NÃO pode variar.
"""
import math

import pytest
from pyproj import Geod

from backend.geo import (
    TILE_PX, METERS_PER_PX, TILE_METERS,
    bbox_from_center, offset_center, ground_distance_m,
)

_GEOD = Geod(ellps="WGS84")

# Tolerância: pyproj.Geod.fwd/.inv arredondam no WGS84; < 1 mm é mais que
# suficiente para um dataset de rótulos a 2.5 m/pixel.
TOL_M = 0.001


def test_constants_consistent():
    assert TILE_PX == 256
    assert METERS_PER_PX == 2.5
    assert TILE_METERS == 640.0


@pytest.mark.parametrize("lat,lon", [
    (0.0, 0.0),             # equador
    (-23.55, -46.63),       # São Paulo
    (45.0, 10.0),           # latitude média norte
    (60.0, 0.0),            # latitude alta
    (-70.0, 120.0),         # antártica
])
def test_tile_ground_dimensions_exactly_640m(lat, lon):
    """Distância geodésica entre centros das bordas N↔S e E↔W = 640 m."""
    w, s, e, n = bbox_from_center(lat, lon)
    # Centro das bordas (meio da aresta, não canto)
    mid_lon = (w + e) / 2.0
    ns_dist = ground_distance_m(s, mid_lon, n, mid_lon)
    ew_dist = ground_distance_m(lat, w, lat, e)
    assert abs(ns_dist - TILE_METERS) < TOL_M, f"NS={ns_dist}"
    assert abs(ew_dist - TILE_METERS) < TOL_M, f"EW={ew_dist}"


@pytest.mark.parametrize("lat,lon", [(0.0, 0.0), (-23.55, -46.63), (60.0, 0.0)])
def test_pixel_is_exactly_2_5m_at_center_latitude(lat, lon):
    """Um pixel no meio do tile deve ter 2.5 m (ambos eixos) na latitude
    do centro, coerente com o `from_bounds` do rasterio (que assume pixels
    uniformes em graus)."""
    w, s, e, n = bbox_from_center(lat, lon)
    lon_per_px = (e - w) / TILE_PX
    lat_per_px = (n - s) / TILE_PX
    # converter um pixel (em graus) para metros na latitude central do tile
    px_ew = ground_distance_m(lat, w, lat, w + lon_per_px)
    px_ns = ground_distance_m(lat - lat_per_px / 2, 0, lat + lat_per_px / 2, 0)
    assert abs(px_ew - METERS_PER_PX) < TOL_M, f"px EW = {px_ew}"
    assert abs(px_ns - METERS_PER_PX) < TOL_M, f"px NS = {px_ns}"


def test_bbox_is_symmetric_around_center():
    """O centro informado deve ser exatamente o centro geodésico do bbox."""
    lat, lon = -23.55, -46.63
    w, s, e, n = bbox_from_center(lat, lon)
    # distância do centro até cada borda, medido no meridiano/paralelo do centro
    d_n = ground_distance_m(lat, lon, n, lon)
    d_s = ground_distance_m(lat, lon, s, lon)
    d_e = ground_distance_m(lat, lon, lat, e)
    d_w = ground_distance_m(lat, lon, lat, w)
    half = TILE_METERS / 2
    for d in (d_n, d_s, d_e, d_w):
        assert abs(d - half) < TOL_M


def test_adjacent_tiles_share_edges_no_gap_no_overlap():
    """Um bloco 3×3 gerado por offset_center deve ter arestas coincidentes:
    a borda leste do tile central = borda oeste do vizinho à direita."""
    lat, lon = -23.55, -46.63
    center = bbox_from_center(lat, lon)
    # vizinho à direita
    lat_r, lon_r = offset_center(lat, lon, 1, 0)
    right = bbox_from_center(lat_r, lon_r)
    # west do vizinho deve igualar east do centro (na latitude da aresta)
    edge_gap = ground_distance_m(lat, center[2], lat, right[0])
    assert edge_gap < TOL_M, f"gap EW = {edge_gap}"
    # vizinho acima
    lat_u, lon_u = offset_center(lat, lon, 0, 1)
    up = bbox_from_center(lat_u, lon_u)
    edge_gap_ns = ground_distance_m(center[3], lon, up[1], lon)
    assert edge_gap_ns < TOL_M, f"gap NS = {edge_gap_ns}"


def test_block_3x3_covers_exactly_1920m_square():
    """3 tiles colados = 3 × 640 = 1920 m em cada eixo, sem sobreposição."""
    lat, lon = 10.0, 20.0
    # canto extremo oeste-sul
    lat_sw, lon_sw = offset_center(lat, lon, -1, -1)
    sw = bbox_from_center(lat_sw, lon_sw)
    # canto extremo leste-norte
    lat_ne, lon_ne = offset_center(lat, lon, 1, 1)
    ne = bbox_from_center(lat_ne, lon_ne)
    total_ew = ground_distance_m(lat, sw[0], lat, ne[2])
    total_ns = ground_distance_m(sw[1], lon, ne[3], lon)
    # tolerância relativa mais generosa: acumulamos 3 chamadas Geod.fwd
    assert abs(total_ew - 3 * TILE_METERS) < 0.01
    assert abs(total_ns - 3 * TILE_METERS) < 0.01


def test_from_bounds_geotiff_transform_matches_2_5m():
    """Simula o que o export_tiles faz: rasterio.from_bounds produz um
    transform cujo |pixel_size| em metros bate com 2.5 m na latitude do tile.
    """
    from rasterio.transform import from_bounds
    lat, lon = -23.55, -46.63
    w, s, e, n = bbox_from_center(lat, lon)
    t = from_bounds(w, s, e, n, TILE_PX, TILE_PX)
    # t.a = pixel width em graus de lon; t.e = pixel height em graus de lat (negativo)
    px_lon_deg = t.a
    px_lat_deg = abs(t.e)
    ew = ground_distance_m(lat, w, lat, w + px_lon_deg)
    ns = ground_distance_m(lat - px_lat_deg / 2, 0, lat + px_lat_deg / 2, 0)
    assert abs(ew - METERS_PER_PX) < TOL_M
    assert abs(ns - METERS_PER_PX) < TOL_M


def test_import_points_script_inserts_with_correct_bbox(tmp_path, monkeypatch):
    """Integração: o script grava no DB uma bbox consistente com a invariante."""
    import os, sys, yaml
    from backend import config as cfg

    # DB temporário isolado
    db = tmp_path / "t.db"
    cfg_path = tmp_path / "config.yaml"
    cfg_data = {
        "tileserver": {"url_template": "x"},
        "classes": [{"id": i, "name": f"c{i}", "color": "#ffffff"} for i in range(1, 7)],
        "tiles": {"source": "x"},
        "auth": {"jwt_secret": "s", "access_token_expiry_hours": 8, "refresh_token_expiry_hours": 24},
        "database": {"path": str(db)},
    }
    cfg_path.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")
    monkeypatch.setenv("TILECLASS_CONFIG", str(cfg_path))
    cfg._CACHE = None  # type: ignore[attr-defined]
    import backend.database as dbmod
    dbmod._DB_PATH = None  # type: ignore[attr-defined]

    from backend.scripts import import_points
    sys.argv = ["import_points", "--point", "-23.55", "-46.63", "sp"]
    import_points.main()

    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT bbox_west, bbox_south, bbox_east, bbox_north FROM tiles WHERE name='sp'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    ns = ground_distance_m(row["bbox_south"], (row["bbox_west"]+row["bbox_east"])/2,
                           row["bbox_north"], (row["bbox_west"]+row["bbox_east"])/2)
    ew = ground_distance_m(-23.55, row["bbox_west"], -23.55, row["bbox_east"])
    assert abs(ns - TILE_METERS) < TOL_M
    assert abs(ew - TILE_METERS) < TOL_M
