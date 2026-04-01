"""
SafeRoute Safety Scoring Engine

Computes 0-100 safety scores for route segments using:
  - Safecity incident density (weighted by severity + hour proximity)
  - Time-of-day multiplier
  - User-submitted incident reports (SQLite)
"""

import logging
import math
import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, Generator, List, Tuple

from data.loader import CATEGORY_LABELS, CATEGORY_WEIGHTS, get_incidents

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — replaces all scattered magic numbers
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "user_reports.db")

INCIDENT_RADIUS_KM:   float = 0.5
DATASET_WEIGHT:       float = 1.0
USER_REPORT_WEIGHT:   float = 1.2   # user reports weighted slightly above dataset

NIGHT_PENALTY:        float = 1.45  # 22:00 – 04:59
EVENING_PENALTY:      float = 1.20  # 18:00 – 21:59
MORNING_BONUS:        float = 0.85  # 06:00 – 08:59

RISK_SIGMOID_SCALE:   float = 1.8
COMPOSITE_AVG_WEIGHT: float = 0.50
COMPOSITE_MIN_WEIGHT: float = 0.50

SAFE_THRESHOLD:       int   = 80
CAUTION_THRESHOLD:    int   = 62

HOUR_TIGHT_WINDOW:    int   = 2     # ≤ 2 h diff → full weight
HOUR_MID_WINDOW:      int   = 4     # ≤ 4 h diff → partial weight
HOUR_FACTOR_TIGHT:    float = 1.0
HOUR_FACTOR_MID:      float = 0.65
HOUR_FACTOR_FAR:      float = 0.3

ROUTE_STEPS:          int   = 60    # interpolation sample count
SEGMENT_CHUNKS:       int   = 20    # colour-overlay segments sent to frontend
MAX_ALERTS:           int   = 8

COLOR_SAFE:    str = "#4CAF82"
COLOR_CAUTION: str = "#E8A838"
COLOR_DANGER:  str = "#E05252"
COLOR_SAFE_MIN:    int = 70
COLOR_CAUTION_MIN: int = 45


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def _db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection, commit on success, rollback + close always."""
    con = sqlite3.connect(DB_PATH)
    try:
        yield con
        con.commit()
    except sqlite3.Error:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Create tables if they don't exist. Call once at startup."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_connection() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lat         REAL        NOT NULL,
                lng         REAL        NOT NULL,
                area        TEXT,
                category    TEXT,
                description TEXT,
                hour        INTEGER,
                created_at  TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def get_user_reports() -> List[Dict]:
    """Load all user-submitted reports in a single DB round-trip."""
    try:
        with _db_connection() as con:
            rows = con.execute(
                "SELECT lat, lng, area, category, description, hour FROM user_reports"
            ).fetchall()
        return [
            {
                "lat": r[0], "lng": r[1], "area": r[2],
                "category": r[3], "description": r[4],
                "hour": r[5], "reports": 1, "source": "user",
            }
            for r in rows
        ]
    except sqlite3.Error:
        logger.exception("Failed to read user_reports from DB")
        return []


def save_user_report(
    lat: float, lng: float, area: str,
    category: str, description: str, hour: int,
) -> None:
    """Insert one user report. init_db() must have been called at startup."""
    with _db_connection() as con:
        con.execute(
            "INSERT INTO user_reports (lat, lng, area, category, description, hour) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lat, lng, area, category, description, hour),
        )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl   = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def interpolate_route(
    coords: List[List[float]], steps: int = ROUTE_STEPS
) -> List[Tuple[float, float]]:
    """
    coords: [[lng, lat], …]  (ORS GeoJSON order)
    Returns [(lat, lng), …] sampled at roughly equal intervals.
    """
    if not coords:
        return []

    points: List[Tuple[float, float]] = []
    seg_steps = max(2, steps // max(1, len(coords) - 1))

    for i in range(len(coords) - 1):
        a_lng, a_lat = coords[i]
        b_lng, b_lat = coords[i + 1]
        for t in range(seg_steps):
            f = t / seg_steps
            points.append((a_lat + f * (b_lat - a_lat), a_lng + f * (b_lng - a_lng)))

    points.append((coords[-1][1], coords[-1][0]))
    return points


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def _hour_factor(travel_hour: int, incident_hour: int) -> float:
    diff = min(abs(travel_hour - incident_hour), 24 - abs(travel_hour - incident_hour))
    if diff <= HOUR_TIGHT_WINDOW:
        return HOUR_FACTOR_TIGHT
    if diff <= HOUR_MID_WINDOW:
        return HOUR_FACTOR_MID
    return HOUR_FACTOR_FAR


def _time_of_day_multiplier(hour: int) -> float:
    if hour >= 22 or hour <= 4:
        return NIGHT_PENALTY
    if 18 <= hour < 22:
        return EVENING_PENALTY
    if 6 <= hour < 9:
        return MORNING_BONUS
    return 1.0


def _segment_color(score: float) -> str:
    if score >= COLOR_SAFE_MIN:
        return COLOR_SAFE
    if score >= COLOR_CAUTION_MIN:
        return COLOR_CAUTION
    return COLOR_DANGER


def score_point(
    lat: float,
    lng: float,
    hour: int,
    incidents: List[Dict],
    user_reports: List[Dict],
    radius_km: float = INCIDENT_RADIUS_KM,
) -> Dict:
    """Score a single (lat, lng) point. Both data lists are passed in — no DB I/O here."""
    total_risk = 0.0
    nearby: List[Dict] = []

    for source, weight in [(incidents, DATASET_WEIGHT), (user_reports, USER_REPORT_WEIGHT)]:
        for inc in source:
            dist = haversine_km(lat, lng, inc["lat"], inc["lng"])
            if dist > radius_km:
                continue

            decay      = 1.0 - (dist / radius_km) ** 0.6
            hour_fac   = _hour_factor(hour, inc["hour"])
            count_amp  = 1.0 + math.log1p(inc.get("reports", 1)) * 0.25
            cat_weight = CATEGORY_WEIGHTS.get(inc["category"], 0.4)

            total_risk += cat_weight * decay * hour_fac * count_amp * weight
            nearby.append(
                {
                    "area":           inc.get("area", "Unknown"),
                    "category":       inc["category"],
                    "category_label": CATEGORY_LABELS.get(inc["category"], inc["category"]),
                    "reports":        inc.get("reports", 1),
                    "distance_m":     round(dist * 1000),
                    "description":    inc.get("description", ""),
                    "source":         inc.get("source", "safecity"),
                }
            )

    total_risk *= _time_of_day_multiplier(hour)
    safety = max(0, min(100, round(100 * math.exp(-total_risk * RISK_SIGMOID_SCALE))))
    return {"safety": safety, "risk": round(total_risk, 4), "nearby": nearby}


def score_route(coords: List[List[float]], hour: int) -> Dict:
    """
    Score a full route.

    coords: [[lng, lat], …]  (GeoJSON order from ORS)
    hour:   0-23

    Data is fetched ONCE here and passed down — not re-fetched per point.
    """
    incidents    = get_incidents()
    user_reports = get_user_reports()       # single DB open for the whole call

    route_points = interpolate_route(coords)
    if not route_points:
        return {"error": "Empty route"}

    point_results = [
        score_point(lat, lng, hour, incidents, user_reports)
        for lat, lng in route_points
    ]

    scores    = [p["safety"] for p in point_results]
    avg_score = round(sum(scores) / len(scores))
    min_score = min(scores)
    composite = round(COMPOSITE_AVG_WEIGHT * avg_score + COMPOSITE_MIN_WEIGHT * min_score)

    # Segment colour overlay for frontend
    chunk = max(1, len(scores) // SEGMENT_CHUNKS)
    segment_colors = []
    for i in range(0, len(scores), chunk):
        seg_avg = sum(scores[i : i + chunk]) / len(scores[i : i + chunk])
        idx = min(i, len(route_points) - 1)
        segment_colors.append(
            {
                "lat":   route_points[idx][0],
                "lng":   route_points[idx][1],
                "score": round(seg_avg),
                "color": _segment_color(seg_avg),
            }
        )

    # Deduplicate alerts — keep closest occurrence per (area, category) pair
    seen: Dict[str, Dict] = {}
    for p in point_results:
        for inc in p["nearby"]:
            key = f"{inc['area']}|{inc['category']}"
            if key not in seen or seen[key]["distance_m"] > inc["distance_m"]:
                seen[key] = inc

    alerts = sorted(
        seen.values(),
        key=lambda x: (-CATEGORY_WEIGHTS.get(x["category"], 0.4), x["distance_m"]),
    )[:MAX_ALERTS]

    cat_counts: Dict[str, int] = {}
    for inc in seen.values():
        label = inc["category_label"]
        cat_counts[label] = cat_counts.get(label, 0) + inc.get("reports", 1)

    # Tier
    if composite >= SAFE_THRESHOLD:
        tier, tier_label = "safe", "Generally Safe"
        advice = "Low incident activity on this route. Stay alert near intersections after 10pm."
    elif composite >= CAUTION_THRESHOLD:
        tier, tier_label = "caution", "Use Caution"
        advice = "Moderate incident history. Prefer well-lit main roads. Avoid isolated lanes."
    else:
        tier, tier_label = "avoid", "Consider Alternatives"
        advice = "Significant reported incidents on this route. We recommend an alternate path."

    # Time note
    if hour >= 22 or hour < 5:
        time_note = "Late night travel significantly increases risk on this route."
    elif 18 <= hour < 22:
        time_note = "Evening hours see higher incident density in this corridor."
    elif 6 <= hour < 9:
        time_note = "Morning commute hours are generally safer with higher foot traffic."
    else:
        time_note = "Daytime travel is lower risk. Stay aware in crowded market areas."

    return {
        "score":                  composite,
        "tier":                   tier,
        "tier_label":             tier_label,
        "advice":                 advice,
        "time_note":              time_note,
        "alerts":                 alerts,
        "category_breakdown":     cat_counts,
        "segment_colors":         segment_colors,
        "total_incidents_nearby": len(seen),
        "points_scored":          len(route_points),
        "avg_score":              avg_score,
        "min_stretch_score":      min_score,
        "data_source": (
            "safecity_github"
            if any(i.get("source") == "safecity" for i in incidents[:5])
            else "local_enriched"
        ),
    }


def get_all_incidents_geojson() -> Dict:
    """All incidents as a GeoJSON FeatureCollection for the heatmap overlay."""
    incidents    = get_incidents()
    user_reports = get_user_reports()

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type":        "Point",
                "coordinates": [inc["lng"], inc["lat"]],
            },
            "properties": {
                "area":           inc.get("area", ""),
                "category":       inc.get("category", "other"),
                "category_label": CATEGORY_LABELS.get(inc.get("category", ""), "Other"),
                "reports":        inc.get("reports", 1),
                "hour":           inc.get("hour", 20),
                "source":         inc.get("source", "safecity"),
                "weight": (
                    CATEGORY_WEIGHTS.get(inc.get("category", ""), 0.4)
                    * inc.get("reports", 1)
                ),
            },
        }
        for inc in (incidents + user_reports)
    ]
    return {"type": "FeatureCollection", "features": features}