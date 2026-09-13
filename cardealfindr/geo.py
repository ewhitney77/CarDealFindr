"""
Distance helpers.

Sources give us dealer location in different forms:
  * MarketCheck   -> dealer latitude/longitude AND a ready-made `dist` field
  * Auto.dev      -> usually city/state/zip, sometimes lat/lon
  * CarGurus      -> city/state, sometimes a distance
  * Dealer sites  -> nothing reliable; we fall back to the group's home state

Strategy: if we have lat/lon, compute haversine distance to HOME. Otherwise
try to geocode the ZIP with `pgeocode` (offline, uses a GeoNames file that is
downloaded once and cached in ~/.cache/pgeocode). If that fails, distance is
unknown and the scorer uses DISTANCE_UNKNOWN_SCORE.
"""
from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Optional

import config

log = logging.getLogger(__name__)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def _nominatim():
    """Lazily build the pgeocode lookup (it loads a ~1 MB file the first time)."""
    try:
        import pgeocode  # noqa: WPS433 (local import keeps startup fast)
        return pgeocode.Nominatim("us")
    except Exception as exc:  # pragma: no cover - depends on network/cache
        log.warning("pgeocode unavailable (%s); ZIP geocoding disabled", exc)
        return None


@lru_cache(maxsize=4096)
def zip_to_latlon(zip_code: Optional[str]) -> Optional[tuple[float, float]]:
    """US ZIP -> (lat, lon), or None if unknown."""
    if not zip_code:
        return None
    z = str(zip_code).strip()[:5]
    if not z.isdigit() or len(z) != 5:
        return None
    nomi = _nominatim()
    if nomi is None:
        return None
    try:
        rec = nomi.query_postal_code(z)
        lat, lon = float(rec.latitude), float(rec.longitude)
        if math.isnan(lat) or math.isnan(lon):
            return None
        return (lat, lon)
    except Exception as exc:  # pragma: no cover
        log.debug("geocode failed for %s: %s", z, exc)
        return None


def distance_from_home(lat: Optional[float], lon: Optional[float],
                       zip_code: Optional[str] = None) -> Optional[float]:
    """
    Miles from HOME_ZIP. Prefers explicit lat/lon; falls back to ZIP centroid.
    Returns None when neither is usable.
    """
    if lat is not None and lon is not None:
        return round(haversine_miles(config.HOME_LAT, config.HOME_LON, lat, lon), 1)
    ll = zip_to_latlon(zip_code)
    if ll:
        return round(haversine_miles(config.HOME_LAT, config.HOME_LON, ll[0], ll[1]), 1)
    return None
