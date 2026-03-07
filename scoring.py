"""
SafeRoute Safety Scoring Engine
Computes 0-100 safety scores for route segments using:
  - Safecity incident density (weighted by severity + recency of hour)
  - Time-of-day multiplier
  - User-submitted incident reports (from SQLite)
"""

import math
import sqlite3
import os
from typing import List, Dict, Tuple
from data.loader import get_incidents, CATEGORY_WEIGHTS, CATEGORY_LABELS

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "user_reports.db")


# ── DB init for user-submitted reports ───────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lat REAL, lng REAL,
            area TEXT,
            category TEXT,
            description TEXT,
            hour INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()


def get_user_reports() -> list:
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT lat, lng, area, category, description, hour FROM user_reports"
        ).fetchall()
        con.close()
        return [
            {"lat": r[0], "lng": r[1], "area": r[2], "category": r[3],
             "description": r[4], "hour": r[5], "reports": 1, "source": "user"}
            for r in rows
        ]
    except Exception:
        return []


def save_user_report(lat: float, lng: float, area: str,
                     category: str, description: str, hour: int):
    init_db()
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO user_reports (lat, lng, area, category, description, hour) VALUES (?,?,?,?,?,?)",
        (lat, lng, area, category, description, hour)
    )
    con.commit()
    con.close()


# ── Geometry helpers ──────────────────────────────────────────────────────────
def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def interpolate_route(coords: List[List[float]], steps: int = 60) -> List[Tuple[float, float]]:
    """
    coords: list of [lng, lat] (ORS GeoJSON order)
    Returns list of (lat, lng) tuples at ~every 100m along route.
    """
    if not coords:
        return []
    points = []
    for i in range(len(coords) - 1):
        a_lng, a_lat = coords[i]
        b_lng, b_lat = coords[i + 1]
        seg_steps = max(2, steps // max(1, len(coords) - 1))
        for t in range(seg_steps):
            f = t / seg_steps
            points.append((
                a_lat + f * (b_lat - a_lat),
                a_lng + f * (b_lng - a_lng),
            ))
    points.append((coords[-1][1], coords[-1][0]))
    return points


# ── Core scoring ──────────────────────────────────────────────────────────────
def score_point(lat: float, lng: float, hour: int,
                incidents: list, user_reports: list,
                radius_km: float = 0.35) -> Dict:
    """Score a single lat/lng point."""
    total_risk = 0.0
    nearby = []

    all_sources = [
        (incidents, 1.0),      # Safecity data weight
        (user_reports, 1.2),   # User reports weighted slightly higher (recent, local)
    ]

    for source, source_weight in all_sources:
        for inc in source:
            dist = haversine_km(lat, lng, inc["lat"], inc["lng"])
            if dist > radius_km:
                continue

            # Distance decay: closer = more risk
            decay = 1.0 - (dist / radius_km) ** 0.6

            # Hour proximity: incidents at similar hours weigh more
            hour_diff = min(abs(hour - inc["hour"]), 24 - abs(hour - inc["hour"]))
            hour_factor = 1.0 if hour_diff <= 2 else (0.65 if hour_diff <= 4 else 0.3)

            # Report count amplifier (log scale)
            count_amp = 1.0 + math.log1p(inc.get("reports", 1)) * 0.25

            # Category severity weight
            cat_weight = CATEGORY_WEIGHTS.get(inc["category"], 0.4)

            risk = cat_weight * decay * hour_factor * count_amp * source_weight
            total_risk += risk

            nearby.append({
                "area": inc.get("area", "Unknown"),
                "category": inc["category"],
                "category_label": CATEGORY_LABELS.get(inc["category"], inc["category"]),
                "reports": inc.get("reports", 1),
                "distance_m": round(dist * 1000),
                "description": inc.get("description", ""),
                "source": inc.get("source", "safecity"),
            })

    # Time-of-day global modifier
    if 22 <= hour or hour <= 4:
        total_risk *= 1.45   # Night penalty
    elif 18 <= hour < 22:
        total_risk *= 1.20   # Evening penalty
    elif 6 <= hour < 9:
        total_risk *= 0.85   # Morning safer

    # Sigmoid-style compression to 0-100 safety score
    safety = max(0, min(100, round(100 * math.exp(-total_risk * 0.75))))
    return {"safety": safety, "risk": round(total_risk, 4), "nearby": nearby}


def score_route(coords: List[List[float]], hour: int) -> Dict:
    """
    Score a full route.
    coords: [[lng, lat], ...] GeoJSON order from ORS
    hour: 0-23
    Returns full scoring result with per-segment breakdown.
    """
    incidents = get_incidents()
    user_reports = get_user_reports()

    route_points = interpolate_route(coords, steps=60)
    if not route_points:
        return {"error": "Empty route"}

    point_results = [score_point(lat, lng, hour, incidents, user_reports)
                     for lat, lng in route_points]

    scores = [p["safety"] for p in point_results]
    avg_score = round(sum(scores) / len(scores))
    min_score = min(scores)

    # Composite punishes bad stretches
    composite = round(0.60 * avg_score + 0.40 * min_score)

    # Build segment colors for frontend (one color per ~10 points)
    segment_colors = []
    chunk = max(1, len(scores) // 20)
    for i in range(0, len(scores), chunk):
        seg_avg = sum(scores[i:i + chunk]) / len(scores[i:i + chunk])
        if seg_avg >= 70:
            color = "#4CAF82"   # safe green
        elif seg_avg >= 45:
            color = "#E8A838"   # caution amber
        else:
            color = "#E05252"   # danger red
        idx = min(i, len(route_points) - 1)
        segment_colors.append({
            "lat": route_points[idx][0],
            "lng": route_points[idx][1],
            "score": round(seg_avg),
            "color": color,
        })

    # Collect unique incident alerts
    seen: Dict[str, dict] = {}
    for p in point_results:
        for inc in p["nearby"]:
            key = inc["area"] + "|" + inc["category"]
            if key not in seen or seen[key]["distance_m"] > inc["distance_m"]:
                seen[key] = inc

    alerts = sorted(
        seen.values(),
        key=lambda x: (-CATEGORY_WEIGHTS.get(x["category"], 0.4), x["distance_m"])
    )[:8]

    # Category breakdown counts
    cat_counts: Dict[str, int] = {}
    for inc in seen.values():
        label = inc["category_label"]
        cat_counts[label] = cat_counts.get(label, 0) + inc.get("reports", 1)

    # Tier
    if composite >= 72:
        tier, tier_label = "safe", "Generally Safe"
        advice = "Low incident activity on this route. Stay alert near intersections after 10pm."
    elif composite >= 48:
        tier, tier_label = "caution", "Use Caution"
        advice = "Moderate incident history. Prefer well-lit main roads. Avoid isolated lanes."
    else:
        tier, tier_label = "avoid", "Consider Alternatives"
        advice = "Significant reported incidents on this route. We recommend an alternate path."

    # Time note
    if 22 <= hour or hour < 5:
        time_note = "Late night travel significantly increases risk on this route."
    elif 18 <= hour < 22:
        time_note = "Evening hours see higher incident density in this corridor."
    elif 6 <= hour < 9:
        time_note = "Morning commute hours are generally safer with higher foot traffic."
    else:
        time_note = "Daytime travel is lower risk. Stay aware in crowded market areas."

    return {
        "score": composite,
        "tier": tier,
        "tier_label": tier_label,
        "advice": advice,
        "time_note": time_note,
        "alerts": alerts,
        "category_breakdown": cat_counts,
        "segment_colors": segment_colors,
        "total_incidents_nearby": len(seen),
        "points_scored": len(route_points),
        "avg_score": avg_score,
        "min_stretch_score": min_score,
        "data_source": "safecity_github" if any(
            i.get("source") == "safecity" for i in incidents[:5]
        ) else "local_enriched",
    }


def get_all_incidents_geojson() -> Dict:
    """All incidents as GeoJSON FeatureCollection for heatmap."""
    incidents = get_incidents()
    user_reports = get_user_reports()
    all_inc = incidents + user_reports

    features = []
    for inc in all_inc:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [inc["lng"], inc["lat"]]},
            "properties": {
                "area": inc.get("area", ""),
                "category": inc.get("category", "other"),
                "category_label": CATEGORY_LABELS.get(inc.get("category", ""), "Other"),
                "reports": inc.get("reports", 1),
                "hour": inc.get("hour", 20),
                "source": inc.get("source", "safecity"),
                "weight": CATEGORY_WEIGHTS.get(inc.get("category", ""), 0.4) * inc.get("reports", 1),
            }
        })
    return {"type": "FeatureCollection", "features": features}
