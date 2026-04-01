"""
OpenRouteService client.

Fetches real walking/driving routes for Delhi.
"""

import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org/v2"

# Module-level session — reuses TCP connections across geocode + routing calls
_session = requests.Session()


# ---------------------------------------------------------------------------
# Key helper  (returns None, never raises — callers decide what to do)
# ---------------------------------------------------------------------------

def get_ors_key() -> Optional[str]:
    """Return the ORS API key, or None if unset / still a placeholder."""
    key = os.environ.get("ORS_API_KEY", "").strip()
    return key if key and not key.startswith("your_") else None


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(place: str) -> Optional[Dict]:
    """
    Convert a place name to lat/lng using ORS geocoding (Pelias).
    Returns {"lat": float, "lng": float, "label": str} or None.
    """
    key = get_ors_key()
    if not key:
        logger.warning("ORS_API_KEY not set; geocoding skipped.")
        return None

    try:
        r = _session.get(
            "https://api.openrouteservice.org/geocode/search",
            params={
                "api_key":          key,
                "text":             f"{place}, Delhi, India",
                "size":             1,
                "boundary.country": "IND",
            },
            timeout=10,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            return None

        props  = features[0]["properties"]
        coords = features[0]["geometry"]["coordinates"]
        return {
            "lat":   round(coords[1], 6),
            "lng":   round(coords[0], 6),
            "label": props.get("label", place),
        }
    except requests.RequestException:
        logger.exception("Geocoding failed for '%s'", place)
        return None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def get_routes(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    profile: str = "foot-walking",
    alternatives: int = 3,
) -> List[Dict]:
    """
    Fetch up to `alternatives` routes from ORS.
    Returns a list of route dicts with geometry coords and summary.
    Returns an empty list on any failure.
    """
    key = get_ors_key()
    if not key:
        logger.warning("ORS_API_KEY not set; routing skipped.")
        return []

    payload = {
        "coordinates": [
            [origin_lng, origin_lat],
            [dest_lng,   dest_lat],
        ],
        "alternative_routes": {
            "target_count":  alternatives,
            "weight_factor": 1.6,
            "share_factor":  0.6,
        },
        "instructions": False,
        "geometry":     True,
    }

    try:
        r = _session.post(
            f"{ORS_BASE}/directions/{profile}/geojson",
            json=payload,
            headers={
                "Authorization": key,
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        routes = []
        for i, feature in enumerate(data.get("features", [])):
            summary = feature["properties"]["summary"]
            coords  = feature["geometry"]["coordinates"]   # [[lng, lat], …]
            routes.append(
                {
                    "index":       i,
                    "coords":      coords,
                    "distance_m":  round(summary.get("distance", 0)),
                    "duration_s":  round(summary.get("duration", 0)),
                    "distance_km": round(summary.get("distance", 0) / 1000, 2),
                    "duration_min": round(summary.get("duration", 0) / 60),
                }
            )
        return routes

    except requests.RequestException:
        logger.exception("ORS routing request failed")
        return []