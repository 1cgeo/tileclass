"""Shared helpers for Web Mercator (EPSG:3857) XYZ tile grids.

Used by the MBTiles builder, the XYZ pyramid builder, and import scripts that
need to reason about tiles in the standard XYZ scheme (origin NW, y growing S).
"""
from __future__ import annotations

import math

from rasterio.crs import CRS

TILE = 256
EARTH = 20037508.342789244  # Web Mercator half-circumference in meters


def force_crs_3857(src_crs) -> CRS:
    """LOCAL_CS Pseudo-Mercator → EPSG:3857.

    The MI 1:25k rasters produced by the 6c pipeline come tagged as
    `LOCAL_CS["WGS 84 / Pseudo-Mercator"]` (EngineeringCRS with no datum),
    which pyproj refuses to transform. Force the canonical EPSG code.
    """
    if src_crs is None:
        return CRS.from_epsg(3857)
    wkt = src_crs.to_wkt() if hasattr(src_crs, "to_wkt") else str(src_crs)
    if "Pseudo-Mercator" in wkt or "LOCAL_CS" in wkt.upper():
        return CRS.from_epsg(3857)
    return src_crs


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in meters (Web Mercator) for tile (z,x,y)."""
    n = 2 ** z
    res = (2 * EARTH) / n
    west = -EARTH + x * res
    east = west + res
    north = EARTH - y * res
    south = north - res
    return west, south, east, north


def tiles_for_bbox(z: int, w: float, s: float, e: float, n: float) -> tuple[int, int, int, int]:
    """XYZ range (xmin,ymin,xmax,ymax) covering bbox in meters (Web Mercator)."""
    nt = 2 ** z
    res = (2 * EARTH) / nt
    xmin = max(0, int((w + EARTH) / res))
    xmax = min(nt - 1, int((e + EARTH) / res))
    ymin = max(0, int((EARTH - n) / res))
    ymax = min(nt - 1, int((EARTH - s) / res))
    return xmin, ymin, xmax, ymax


def merc_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Web Mercator meters → WGS84 (lon, lat) degrees."""
    lon = x / EARTH * 180.0
    lat = (math.atan(math.exp(y / EARTH * math.pi)) * 2 - math.pi / 2) * 180.0 / math.pi
    return lon, lat
