"""Geodesic helpers: every tile is 256×256 pixels @ 2.5 m/pixel = 640 m × 640 m
on the ground, regardless of latitude. Bounding boxes are computed from the
center using WGS84 geodesic math (pyproj.Geod), guaranteeing uniform ground
size in any location.
"""
from pyproj import Geod

TILE_PX = 256
METERS_PER_PX = 2.5
TILE_METERS = TILE_PX * METERS_PER_PX  # 640 m

_GEOD = Geod(ellps="WGS84")


def bbox_from_center(lat: float, lon: float) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in WGS84 degrees for a tile centered
    on (lat, lon). The tile spans exactly TILE_METERS in both cardinal
    directions, measured as geodesic distance from the center.
    """
    half = TILE_METERS / 2.0
    lon_n, lat_n, _ = _GEOD.fwd(lon, lat, 0.0,   half)   # north
    lon_s, lat_s, _ = _GEOD.fwd(lon, lat, 180.0, half)   # south
    lon_e, lat_e, _ = _GEOD.fwd(lon, lat, 90.0,  half)   # east
    lon_w, lat_w, _ = _GEOD.fwd(lon, lat, 270.0, half)   # west
    return (lon_w, lat_s, lon_e, lat_n)


def offset_center(lat: float, lon: float, dx_tiles: int, dy_tiles: int) -> tuple[float, float]:
    """Return a new center (lat', lon') offset by (dx_tiles, dy_tiles) full
    tile-widths of TILE_METERS, so that adjacent tiles share edges exactly."""
    lat_cur, lon_cur = lat, lon
    if dy_tiles != 0:
        az = 0.0 if dy_tiles > 0 else 180.0
        lon_cur, lat_cur, _ = _GEOD.fwd(lon_cur, lat_cur, az, TILE_METERS * abs(dy_tiles))
    if dx_tiles != 0:
        az = 90.0 if dx_tiles > 0 else 270.0
        lon_cur, lat_cur, _ = _GEOD.fwd(lon_cur, lat_cur, az, TILE_METERS * abs(dx_tiles))
    return (lat_cur, lon_cur)


def ground_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Geodesic distance in meters between two WGS84 points."""
    _, _, dist = _GEOD.inv(lon1, lat1, lon2, lat2)
    return dist
