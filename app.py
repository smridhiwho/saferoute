"""
SafeRoute Flask App

Endpoints:
    GET  /                      Serve frontend
    GET  /api/health            Health check
    GET  /api/incidents         All incidents as GeoJSON
    POST /api/geocode           Geocode a place name
    POST /api/route             Get scored route options
    POST /api/report            Submit a user incident report
    GET  /api/stats             Dashboard stats
    POST /api/whatsapp/simulate Simulate WhatsApp bot query
"""

import json
import logging
import os
import re
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

from ai_advisor import get_ai_safety_advice, get_ai_whatsapp_reply, get_groq_key
from data.loader import CATEGORY_LABELS, CATEGORY_WEIGHTS, get_incidents
from ors_client import geocode, get_routes
from scoring import get_all_incidents_geojson, init_db, save_user_report, score_route

# ---------------------------------------------------------------------------
# Logging — replaces all print() calls
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static")
app.config["JSON_SORT_KEYS"] = False

init_db()   # create tables once at startup — NOT inside save_user_report

logger.info("Pre-loading incident dataset…")
_incidents = get_incidents()
logger.info("Ready. %d incidents loaded.", len(_incidents))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _key_is_set(env_var: str) -> bool:
    val = os.environ.get(env_var, "").strip()
    return bool(val) and not val.startswith("your_")


def _resolve_coords(lat, lng, name: str):
    """
    Return (lat, lng, resolved_name).
    Uses explicit coords if both present; falls back to geocoding name.
    """
    if lat is not None and lng is not None:
        return float(lat), float(lng), name
    if name:
        try:
            geo = geocode(name)
        except Exception:
            logger.exception("Geocoding raised unexpectedly for '%s'", name)
            return None, None, name
        if geo:
            return geo["lat"], geo["lng"], geo.get("label", name)
    return None, None, name


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    incidents = get_incidents()
    return jsonify(
        {
            "status":          "ok",
            "service":         "SafeRoute API",
            "incidents_loaded": len(incidents),
            "ors_configured":  _key_is_set("ORS_API_KEY"),
            "groq_configured": _key_is_set("GROQ_API_KEY"),
        }
    )


# ---------------------------------------------------------------------------
# Incidents GeoJSON
# ---------------------------------------------------------------------------

@app.route("/api/incidents")
def incidents():
    return jsonify(get_all_incidents_geojson())


# ---------------------------------------------------------------------------
# Geocode
# ---------------------------------------------------------------------------

@app.route("/api/geocode", methods=["POST"])
def geocode_place():
    body  = request.get_json(silent=True) or {}
    place = body.get("place", "").strip()
    if not place:
        return jsonify({"error": "place required"}), 400

    try:
        result = geocode(place)
    except Exception:
        logger.exception("Geocoding service error for '%s'", place)
        return jsonify({"error": "Geocoding service unavailable"}), 503

    if not result:
        return jsonify({"error": f"Could not geocode '{place}'"}), 404
    return jsonify(result)


# ---------------------------------------------------------------------------
# Main route scoring
# ---------------------------------------------------------------------------

@app.route("/api/route", methods=["POST"])
def get_route():
    """
    Body:
        origin / destination  (str)    — optional if coords provided
        origin_lat, origin_lng (float) — skip geocoding when supplied
        dest_lat, dest_lng     (float)
        hour   (int 0-23)              — defaults to current hour
        profile ("foot-walking" | "driving-car")
    """
    body    = request.get_json(silent=True) or {}
    hour    = _clamp(int(body.get("hour", datetime.now().hour)), 0, 23)
    profile = body.get("profile", "foot-walking")

    origin_name = body.get("origin", "").strip()
    dest_name   = body.get("destination", "").strip()

    origin_lat, origin_lng, origin_name = _resolve_coords(
        body.get("origin_lat"), body.get("origin_lng"), origin_name
    )
    dest_lat, dest_lng, dest_name = _resolve_coords(
        body.get("dest_lat"), body.get("dest_lng"), dest_name
    )

    if not all([origin_lat, origin_lng, dest_lat, dest_lng]):
        return jsonify({"error": "Could not resolve coordinates"}), 400

    routes = get_routes(
        float(origin_lat), float(origin_lng),
        float(dest_lat),   float(dest_lng),
        profile=profile,
        alternatives=3,
    )
    if not routes:
        return jsonify({"error": "Could not fetch routes. Check your ORS API key."}), 503

    results = []
    for route_data in routes:
        scored = score_route(route_data["coords"], hour)
        scored.update(
            {
                "route_index":  route_data["index"],
                "coords":       route_data["coords"],
                "distance_km":  route_data["distance_km"],
                "distance_m":   route_data["distance_m"],
                "duration_min": route_data["duration_min"],
                "duration_s":   route_data["duration_s"],
            }
        )
        # AI advisory only for the top route to conserve API quota
        if route_data["index"] == 0:
            scored["ai_advice"] = get_ai_safety_advice(scored, origin_name, dest_name, hour)
        results.append(scored)

    results.sort(key=lambda r: r["score"], reverse=True)

    _LABELS = ["Safest Route", "Alternative 1", "Alternative 2"]
    for i, r in enumerate(results):
        r["label"] = _LABELS[i] if i < len(_LABELS) else f"Route {i + 1}"

    return jsonify(
        {
            "origin":      {"name": origin_name, "lat": origin_lat, "lng": origin_lng},
            "destination": {"name": dest_name,   "lat": dest_lat,   "lng": dest_lng},
            "hour":        hour,
            "profile":     profile,
            "routes":      results,
        }
    )


# ---------------------------------------------------------------------------
# Report incident
# ---------------------------------------------------------------------------

@app.route("/api/report", methods=["POST"])
def report_incident():
    body        = request.get_json(silent=True) or {}
    lat         = body.get("lat")
    lng         = body.get("lng")
    description = body.get("description", "").strip()

    if lat is None or lng is None:
        return jsonify({"error": "lat and lng required"}), 400
    if not description:
        return jsonify({"error": "description required"}), 400

    # Validate coordinate ranges
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return jsonify({"error": "Invalid coordinates"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lng must be numbers"}), 400

    category = body.get("category", "other").strip()
    if category not in CATEGORY_WEIGHTS:
        category = "other"

    save_user_report(
        lat=lat,
        lng=lng,
        area=body.get("area", "Delhi").strip(),
        category=category,
        description=description,
        hour=_clamp(int(body.get("hour", datetime.now().hour)), 0, 23),
    )
    return jsonify(
        {"success": True, "message": "Report submitted. Thank you for keeping the community safe."}
    )


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def stats():
    incidents = get_incidents()

    cat_counts: dict  = {}
    area_counts: dict = {}
    hour_counts       = [0] * 24

    for inc in incidents:
        label   = CATEGORY_LABELS.get(inc.get("category", ""), "Other")
        reports = inc.get("reports", 1)
        cat_counts[label]  = cat_counts.get(label, 0) + reports

        area = inc.get("area", "Unknown")
        area_counts[area] = area_counts.get(area, 0) + reports

        h = inc.get("hour", 20)
        if 0 <= h <= 23:
            hour_counts[h] += reports

    top_areas = sorted(area_counts.items(), key=lambda x: -x[1])[:10]
    safest    = [a for a in sorted(area_counts.items(), key=lambda x: x[1]) if a[1] < 5][:5]

    return jsonify(
        {
            "total_incidents": len(incidents),
            "total_reports":   sum(i.get("reports", 1) for i in incidents),
            "category_breakdown": [
                {"name": k, "count": v}
                for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])
            ],
            "top_hotspots":  [{"area": a, "reports": c} for a, c in top_areas],
            "safest_areas":  [{"area": a, "reports": c} for a, c in safest],
            "hourly_distribution": [
                {"hour": h, "reports": hour_counts[h]} for h in range(24)
            ],
        }
    )


# ---------------------------------------------------------------------------
# WhatsApp simulator
# ---------------------------------------------------------------------------

@app.route("/api/whatsapp/simulate", methods=["POST"])
def whatsapp_simulate():
    body     = request.get_json(silent=True) or {}
    user_msg = body.get("message", "").strip()

    if not user_msg:
        return jsonify(
            {
                "reply": (
                    "Hi! I'm SafeRoute. Send me a message like:\n"
                    "'Safe route from Lajpat Nagar to Connaught Place at 9pm'"
                )
            }
        )

    parsed = _parse_whatsapp_message(user_msg)
    if not parsed or not parsed.get("origin") or not parsed.get("destination"):
        return jsonify(
            {
                "reply": (
                    "I didn't quite catch that. Try:\n"
                    "'Route from [place] to [place] at [hour]pm'\n"
                    "Example: 'Safe route from Karol Bagh to Saket at 8pm'"
                )
            }
        )

    origin, destination = parsed["origin"], parsed["destination"]
    hour = parsed.get("hour", 20)

    orig_geo = geocode(origin)
    dest_geo = geocode(destination)
    if not orig_geo or not dest_geo:
        return jsonify(
            {"reply": f"Sorry, I couldn't find {origin or destination} in Delhi. Try a more specific name."}
        )

    routes = get_routes(orig_geo["lat"], orig_geo["lng"], dest_geo["lat"], dest_geo["lng"])
    if not routes:
        return jsonify({"reply": "Routing service unavailable right now. Please try again shortly."})

    best   = routes[0]
    scored = score_route(best["coords"], hour)
    scored["distance_km"]  = best["distance_km"]
    scored["duration_min"] = best["duration_min"]

    return jsonify(
        {
            "reply":       get_ai_whatsapp_reply(user_msg, scored, origin, destination, hour),
            "score":       scored["score"],
            "tier":        scored["tier"],
            "origin":      orig_geo,
            "destination": dest_geo,
            "route_coords": best["coords"],
        }
    )


def _parse_whatsapp_message(msg: str) -> dict:
    """
    Parse origin, destination, and hour from a natural-language message.
    Tries Groq NLP first; falls back to regex.
    """
    msg_lower = msg.lower()

    # --- Hour extraction ---
    hour = 20
    TIME_PATTERNS = [
        (r"(\d{1,2})\s*am", lambda m: int(m.group(1)) % 12),
        (r"(\d{1,2})\s*pm", lambda m: int(m.group(1)) % 12 + 12),
        (r"at\s+(\d{1,2})", lambda m: int(m.group(1))),
        (r"(\d{1,2}):(\d{2})", lambda m: int(m.group(1))),
    ]
    for pattern, extractor in TIME_PATTERNS:
        m = re.search(pattern, msg_lower)
        if m:
            hour = _clamp(extractor(m), 0, 23)
            break

    # --- Groq NLP (uses the shared helper from ai_advisor) ---
    from ai_advisor import _call_groq  # local import avoids circular dep at module level
    prompt = (
        f'Extract origin and destination from: "{msg}"\n'
        'Return ONLY valid JSON: {"origin": "place", "destination": "place"}\n'
        'If not found: {"origin": null, "destination": null}'
    )
    groq_text = _call_groq(prompt, max_tokens=60, temperature=0.2)
    if groq_text:
        try:
            parsed = json.loads(groq_text)
            parsed["hour"] = hour
            return parsed
        except (json.JSONDecodeError, KeyError):
            logger.warning("Groq returned non-JSON for message parse: %s", groq_text)

    # --- Regex fallback ---
    m = re.search(r"from\s+([\w\s]+?)\s+to\s+([\w\s]+?)(?:\s+at|\s+around|$)", msg_lower)
    if m:
        return {
            "origin":      m.group(1).strip().title(),
            "destination": m.group(2).strip().title(),
            "hour":        hour,
        }

    return {"origin": None, "destination": None, "hour": hour}


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", 5001)))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    logger.info("SafeRoute running at http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)