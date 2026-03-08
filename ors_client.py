"""
OpenRouteService client.
Fetches real walking/driving routes for Delhi.
"""

import os
import requests
from typing import Optional, Dict, List

ORS_BASE = "https://api.openrouteservice.org/v2"

def get_ors_key() -> str:
    key = os.environ.get("ORS_API_KEY", "")
    if not key or key == "your_ors_key_here":
        raise ValueError("ORS_API_KEY not set. Add it to your .env file.")
    return key


def geocode(place: str) -> Optional[Dict]:
    """
    Convert a place name to lat/lng using ORS geocoding (Pelias).
    Returns {"lat": float, "lng": float, "label": str} or None.
    """
    try:
        r = requests.get(
            "https://api.openrouteservice.org/geocode/search",
            params={
                "api_key": get_ors_key(),
                "text": place + ", Delhi, India",
                "size": 1,
                "boundary.country": "IND",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        features = data.get("features", [])
        if not features:
            return None
        props = features[0]["properties"]
        coords = features[0]["geometry"]["coordinates"]
        return {
            "lat": round(coords[1], 6),
            "lng": round(coords[0], 6),
            "label": props.get("label", place),
        }
    except Exception as e:
        print(f"  Geocoding failed for '{place}': {e}")
        return None


def get_routes(
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
    profile: str = "foot-walking",
    alternatives: int = 3,
) -> List[Dict]:
    """
    Fetch up to `alternatives` routes from ORS.
    Returns list of route dicts with geometry coords and summary.
    """
    try:
        payload = {
            "coordinates": [
                [origin_lng, origin_lat],
                [dest_lng, dest_lat],
            ],
            "alternative_routes": {
                "target_count": alternatives,
                "weight_factor": 1.6,
                "share_factor": 0.6,
            },
            "instructions": True,
            "instructions_format": "text",
            "geometry": True,
        }
        r = requests.post(
            f"{ORS_BASE}/directions/{profile}/geojson",
            json=payload,
            headers={
                "Authorization": get_ors_key(),
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        routes = []
        for i, feature in enumerate(data.get("features", [])):
            summary = feature["properties"]["summary"]
            coords = feature["geometry"]["coordinates"]  # [[lng, lat], ...]
            # Extract turn-by-turn steps
            steps = []
            for seg in feature["properties"].get("segments", []):
                for step in seg.get("steps", []):
                    steps.append({
                        "instruction": step.get("instruction", ""),
                        "name": step.get("name", ""),
                        "distance": step.get("distance", 0),
                        "duration": step.get("duration", 0),
                        "type": step.get("type", 0),
                        "way_points": step.get("way_points", []),
                    })
            routes.append({
                "index": i,
                "coords": coords,
                "steps": steps,
                "distance_m": round(summary.get("distance", 0)),
                "duration_s": round(summary.get("duration", 0)),
                "distance_km": round(summary.get("distance", 0) / 1000, 2),
                "duration_min": round(summary.get("duration", 0) / 60),
            })
        return routes

    except Exception as e:
        print(f"  ORS routing failed: {e}")
        return []
