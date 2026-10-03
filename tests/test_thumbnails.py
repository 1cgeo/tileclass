"""Satellite thumbnail composition: pick the lowest source zoom that still gives
>= `size` pixels across the tile (clamped to the source zoom range) instead of
always composing at max zoom (a 640 m tile at z19 = ~100 source reads)."""
import io
import math

import pytest
from PIL import Image

from backend import geo
from backend.admin import thumbnails


def _png(color=(10, 200, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeReader:
    def __init__(self, zmin, zmax, only_zoom=None):
        self.zr = (zmin, zmax)
        self.only_zoom = only_zoom
        self.reads = []
        self._png = _png()

    def is_open(self):
        return True

    def zoom_range(self):
        return self.zr

    def get_tile(self, z, x, y):
        self.reads.append((z, x, y))
        if self.only_zoom is not None and z != self.only_zoom:
            return None
        return self._png


def _insert_tile(tile_meters=640.0, lat=-20.0, lon=-50.0):
    from backend.database import connect
    from tests.conftest import _seed_test_project
    w, s, e, n = geo.bbox_from_center(lat, lon, tile_meters)
    conn = connect()
    try:
        _seed_test_project(conn)
        cur = conn.execute(
            "INSERT INTO tiles(project_id,name,bbox_west,bbox_south,bbox_east,bbox_north,status) "
            "VALUES (1,'thumb',?,?,?,?,'pending')", (w, s, e, n))
        conn.commit()
        return cur.lastrowid, (w, s, e, n)
    finally:
        conn.close()


def _px_across(bbox, z):
    w, s, e, n = bbox
    return min((e - w) / 360.0 * (1 << z) * 256,
               (thumbnails._lat_to_tile_y(s, z) - thumbnails._lat_to_tile_y(n, z)) * 256)


@pytest.fixture()
def fake(monkeypatch):
    holder = {}

    def install(reader):
        holder["r"] = reader
        monkeypatch.setattr(thumbnails.mbtiles_service, "get_reader",
                            lambda pid, layer: reader)
        return reader
    return install


@pytest.mark.parametrize("size", [64, 128, 256])
def test_thumbnail_uses_lowest_sufficient_zoom(app_env, fake, size):
    tid, bbox = _insert_tile()
    reader = fake(FakeReader(0, 19))
    png = thumbnails._tile_satellite_png(tid, size)
    img = Image.open(io.BytesIO(png))
    assert img.size == (size, size)
    zooms = {z for z, _, _ in reader.reads}
    assert len(zooms) == 1
    z = zooms.pop()
    assert _px_across(bbox, z) >= size          # enough resolution
    assert _px_across(bbox, z - 1) < size       # …and the lowest such zoom
    assert len(reader.reads) <= 9               # was ~100 reads at z19
    assert img.convert("RGB").getpixel((size // 2, size // 2)) == (10, 200, 30)


def test_thumbnail_zoom_clamped_to_source_range(app_env, fake):
    tid, bbox = _insert_tile()
    lo = fake(FakeReader(17, 19))
    thumbnails._tile_satellite_png(tid, 64)
    assert {z for z, _, _ in lo.reads} == {17}
    hi = fake(FakeReader(0, 10))
    png = thumbnails._tile_satellite_png(tid, 128)
    assert {z for z, _, _ in hi.reads} == {10}
    assert Image.open(io.BytesIO(png)).size == (128, 128)


def test_large_tile_reads_stay_bounded(app_env, fake):
    """A huge tile (4096 px × 2.5 m ≈ 10 km) would read thousands of z19 tiles."""
    tid, bbox = _insert_tile(tile_meters=4096 * 2.5)
    reader = fake(FakeReader(0, 19))
    png = thumbnails._tile_satellite_png(tid, 128)
    assert Image.open(io.BytesIO(png)).size == (128, 128)
    assert len(reader.reads) <= 9


def test_thumbnail_falls_back_to_max_zoom_when_pyramid_sparse(app_env, fake):
    """Sources with only the top zoom populated still produce imagery."""
    tid, _ = _insert_tile()
    reader = fake(FakeReader(0, 19, only_zoom=19))
    png = thumbnails._tile_satellite_png(tid, 128)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.getpixel((64, 64)) == (10, 200, 30)
    assert any(z == 19 for z, _, _ in reader.reads)
