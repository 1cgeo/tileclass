"""Teste ponta-a-ponta: para vários pontos no mundo, criar um tile via
`bbox_from_center`, escrever o GeoTIFF usando a mesma pipeline do
`export_tiles.py`, reabrir o raster e validar:
  - CRS = EPSG:4326
  - dimensões = 256×256
  - transform coerente com a bbox original
  - tamanho de pixel em metros = 2.5 (tanto pela borda N/S quanto E/W,
    na latitude do centro) com tolerância < 1 mm
  - distância geodésica entre cantos opostos do raster (canto à canto,
    medida na horizontal/vertical) bate com 640 m
  - georreferenciamento inverso: `dataset.xy(row, col)` para o centro do
    tile retorna as coordenadas do ponto de entrada
"""
import os
# Neste Windows há instalações concorrentes de PROJ (PostgreSQL/PostGIS, pyproj,
# rasterio) cada uma com versão diferente do proj.db. Força o rasterio a usar
# o proj.db dele próprio antes de qualquer operação que toque CRS.
import rasterio as _rio
_RASTERIO_PROJ = os.path.join(os.path.dirname(_rio.__file__), "proj_data")
os.environ["PROJ_LIB"] = _RASTERIO_PROJ
os.environ["PROJ_DATA"] = _RASTERIO_PROJ

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from backend.geo import (
    TILE_PX, METERS_PER_PX, TILE_METERS,
    bbox_from_center, ground_distance_m,
)
from backend.scripts.export_tiles import _write_geotiff


# 20 pontos espalhados por continentes e latitudes: equador, trópicos,
# médias, altas e polares; hemisférios norte e sul; leste e oeste.
WORLD_POINTS = [
    ("equator_atlantic",       0.0,    0.0),
    ("equator_pacific",        0.0,  160.0),
    ("equator_indonesia",      0.5,  110.0),
    ("amazonas",              -3.11, -60.02),
    ("sp_brasil",            -23.55, -46.63),
    ("santiago",             -33.45, -70.67),
    ("cape_town",            -33.92,  18.42),
    ("sydney",               -33.87, 151.21),
    ("buenos_aires",         -34.60, -58.38),
    ("lagos",                  6.52,   3.38),
    ("cairo",                 30.05,  31.24),
    ("tokyo",                 35.68, 139.69),
    ("new_york",              40.71, -74.01),
    ("madrid",                40.42,  -3.70),
    ("beijing",               39.90, 116.40),
    ("berlin",                52.52,  13.40),
    ("moscow",                55.75,  37.62),
    ("reykjavik",             64.13, -21.82),
    ("tromso",                69.65,  18.96),
    ("mcmurdo_antarctica",   -77.85, 166.67),
]


TOL_M = 0.001  # 1 mm


def _make_raster(tmp: Path, name: str, lat: float, lon: float) -> Path:
    """Gera um tile 256×256 (máscara sintética) e escreve o GeoTIFF com a
    mesma pipeline do export_tiles. Retorna o caminho do .tif."""
    w, s, e, n = bbox_from_center(lat, lon)
    arr = (np.arange(TILE_PX * TILE_PX, dtype=np.uint8) % 6 + 1).reshape(TILE_PX, TILE_PX)
    out = tmp / f"{name}.tif"
    _write_geotiff(out, arr, (w, s, e, n))
    return out


@pytest.mark.parametrize("name,lat,lon", WORLD_POINTS, ids=[p[0] for p in WORLD_POINTS])
def test_raster_georeference_worldwide(tmp_path, name, lat, lon):
    tif = _make_raster(tmp_path, name, lat, lon)

    with rasterio.open(tif) as ds:
        # 1) CRS e dimensões
        assert ds.crs is not None
        assert ds.crs.to_epsg() == 4326, f"CRS inesperado: {ds.crs}"
        assert ds.width == TILE_PX and ds.height == TILE_PX
        assert ds.count == 1
        assert ds.dtypes[0] == "uint8"

        # 2) transform coerente com bbox informada
        bounds = ds.bounds  # (left, bottom, right, top)
        w_expected, s_expected, e_expected, n_expected = bbox_from_center(lat, lon)
        assert abs(bounds.left  - w_expected) < 1e-9
        assert abs(bounds.right - e_expected) < 1e-9
        assert abs(bounds.bottom - s_expected) < 1e-9
        assert abs(bounds.top    - n_expected) < 1e-9

        # 3) tamanho de pixel em metros na latitude do centro
        px_lon_deg = ds.transform.a
        px_lat_deg = abs(ds.transform.e)
        px_ew = ground_distance_m(lat, bounds.left, lat, bounds.left + px_lon_deg)
        px_ns = ground_distance_m(lat - px_lat_deg/2, 0, lat + px_lat_deg/2, 0)
        assert abs(px_ew - METERS_PER_PX) < TOL_M, f"{name}: px EW = {px_ew}"
        assert abs(px_ns - METERS_PER_PX) < TOL_M, f"{name}: px NS = {px_ns}"

        # 4) canto-a-canto: ground EW na latitude do centro e ground NS no
        #    meridiano do centro = 640 m (sinal de que a bbox inteira está correta)
        mid_lon = (bounds.left + bounds.right) / 2
        total_ew = ground_distance_m(lat, bounds.left, lat, bounds.right)
        total_ns = ground_distance_m(bounds.bottom, mid_lon, bounds.top, mid_lon)
        assert abs(total_ew - TILE_METERS) < TOL_M, f"{name}: total EW = {total_ew}"
        assert abs(total_ns - TILE_METERS) < TOL_M, f"{name}: total NS = {total_ns}"

        # 5) georreferenciamento inverso: o pixel central do raster deve
        # mapear de volta para (lat, lon) original (com erro de <= 1 px em
        # distância, já que o "centro" exato fica entre os pixels 127 e 128)
        row_c = TILE_PX // 2
        col_c = TILE_PX // 2
        cx_lon, cx_lat = ds.xy(row_c, col_c)
        err_m = ground_distance_m(lat, lon, cx_lat, cx_lon)
        assert err_m < METERS_PER_PX * 1.5, f"{name}: centro off por {err_m}m"


def test_all_rasters_have_identical_footprint_shape(tmp_path):
    """Independente da latitude, o footprint em metros deve ser sempre o mesmo
    quadrado 640×640 m. A variação em GRAUS é esperada (longitude encolhe
    com o cosseno da latitude); a variação em METROS não pode existir."""
    ew_values = []
    ns_values = []
    for name, lat, lon in WORLD_POINTS:
        tif = _make_raster(tmp_path, name, lat, lon)
        with rasterio.open(tif) as ds:
            b = ds.bounds
            mid_lon = (b.left + b.right) / 2
            ew_values.append(ground_distance_m(lat, b.left, lat, b.right))
            ns_values.append(ground_distance_m(b.bottom, mid_lon, b.top, mid_lon))
    # Spread máximo entre os 20 rasters: desprezível
    assert max(ew_values) - min(ew_values) < TOL_M
    assert max(ns_values) - min(ns_values) < TOL_M
    assert all(abs(v - TILE_METERS) < TOL_M for v in ew_values)
    assert all(abs(v - TILE_METERS) < TOL_M for v in ns_values)


def test_longitude_span_in_degrees_shrinks_with_latitude(tmp_path):
    """Sanidade oposta: o span em GRAUS de longitude tem que encolher quando
    a latitude sobe (640m no equador cobre ~0.00575°; a 60° cobre ~0.0115°).
    Se este teste falhar, alguma aproximação esférica voltou a se esconder
    no código."""
    spans_by_lat = []
    for name, lat, lon in WORLD_POINTS:
        tif = _make_raster(tmp_path, name, lat, lon)
        with rasterio.open(tif) as ds:
            b = ds.bounds
            spans_by_lat.append((abs(lat), b.right - b.left))
    spans_by_lat.sort()
    # A maior latitude em valor absoluto deve ter o MAIOR span em graus
    _, smallest_span = spans_by_lat[0]    # perto do equador
    _, largest_span = spans_by_lat[-1]    # perto dos polos
    assert largest_span > smallest_span * 2, (smallest_span, largest_span)
