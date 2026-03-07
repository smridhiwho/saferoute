"""
SafeRoute Flask App (MVP Monolith)
All routes in one file as per MVP/agile approach.

Endpoints:
  GET  /                        Serve frontend
  GET  /api/health              Health check
  GET  /api/incidents           All incidents as GeoJSON
  POST /api/geocode             Geocode a place name
  POST /api/route               Get scored route options
  POST /api/report              Submit a user incident report
  GET  /api/stats               Dashboard stats
  POST /api/whatsapp/simulate   Simulate WhatsApp bot query
"""

import os
import sys

# Load .env if present
def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()

from flask import Flask, request, jsonify, send_from_directory
from scoring import score_route, get_all_incidents_geojson, save_user_report, init_db
from ors_client import geocode, get_routes
from ai_advisor import get_ai_safety_advice, get_ai_whatsapp_reply
from data.loader import get_incidents, CATEGORY_LABELS

app = Flask(__name__, static_folder="static")
app.config["JSON_SORT_KEYS"] = False

# Initialise DB on startup
init_db()

# Pre-load incidents so first request is fast
print("\n  Pre-loading incident dataset...")
_incidents = get_incidents()
print(f"  Ready. {len(_incidents)} incidents loaded.\n")


# ── Static frontend ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    incidents = get_incidents()
    return jsonify({
        "status": "ok",
        "service": "SafeRoute API",
        "incidents_loaded": len(incidents),
        "ors_configured": bool(os.environ.get("ORS_API_KEY", "").strip() not in ("", "your_ors_key_here")),
        "groq_configured": bool(os.environ.get("GROQ_API_KEY", "").strip() not in ("", "your_groq_key_here")),
    })


# ── Incidents GeoJSON ─────────────────────────────────────────────────────────
@app.route("/api/incidents")
def incidents():
    """All incidents as GeoJSON for heatmap/marker overlay."""
    return jsonify(get_all_incidents_geojson())


# ── Geocode ───────────────────────────────────────────────────────────────────
@app.route("/api/geocode", methods=["POST"])
def geocode_place():
    body = request.get_json(silent=True) or {}
    place = body.get("place", "").strip()
    if not place:
        return jsonify({"error": "place required"}), 400
    result = geocode(place)
    if not result:
        return jsonify({"error": f"Could not geocode '{place}'"}), 404
    return jsonify(result)


# ── Main route scoring ────────────────────────────────────────────────────────
@app.route("/api/route", methods=["POST"])
def get_route():
    """
    Score route options between origin and destination.
    Body: {
      "origin": "Lajpat Nagar",       or use origin_lat/origin_lng
      "destination": "Connaught Place",
      "origin_lat": float,            optional, skip geocoding
      "origin_lng": float,
      "dest_lat": float,
      "dest_lng": float,
      "hour": int,                    0-23, default = current hour
      "profile": "foot-walking"       or "driving-car"
    }
    """
    body = request.get_json(silent=True) or {}

    origin_name = body.get("origin", "").strip()
    dest_name = body.get("destination", "").strip()
    hour = int(body.get("hour", __import__("datetime").datetime.now().hour))
    profile = body.get("profile", "foot-walking")

    # Clamp hour
    hour = max(0, min(23, hour))

    # Resolve coordinates
    origin_lat = body.get("origin_lat")
    origin_lng = body.get("origin_lng")
    dest_lat = body.get("dest_lat")
    dest_lng = body.get("dest_lng")

    if not all([origin_lat, origin_lng]) and origin_name:
        geo = geocode(origin_name)
        if not geo:
            return jsonify({"error": f"Could not locate '{origin_name}'"}), 404
        origin_lat, origin_lng = geo["lat"], geo["lng"]
        origin_name = geo.get("label", origin_name)

    if not all([dest_lat, dest_lng]) and dest_name:
        geo = geocode(dest_name)
        if not geo:
            return jsonify({"error": f"Could not locate '{dest_name}'"}), 404
        dest_lat, dest_lng = geo["lat"], geo["lng"]
        dest_name = geo.get("label", dest_name)

    if not all([origin_lat, origin_lng, dest_lat, dest_lng]):
        return jsonify({"error": "Could not resolve coordinates"}), 400

    # Fetch routes from ORS
    routes = get_routes(
        float(origin_lat), float(origin_lng),
        float(dest_lat), float(dest_lng),
        profile=profile,
        alternatives=3,
    )

    if not routes:
        return jsonify({"error": "Could not fetch routes. Check your ORS API key."}), 503

    # Score each route
    results = []
    for route_data in routes:
        scored = score_route(route_data["coords"], hour)
        scored.update({
            "route_index": route_data["index"],
            "coords": route_data["coords"],
            "distance_km": route_data["distance_km"],
            "distance_m": route_data["distance_m"],
            "duration_min": route_data["duration_min"],
            "duration_s": route_data["duration_s"],
        })

        # AI advisory (only for best route to save API calls)
        if route_data["index"] == 0:
            ai_advice = get_ai_safety_advice(scored, origin_name, dest_name, hour)
            scored["ai_advice"] = ai_advice

        results.append(scored)

    # Sort by safety score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # Label routes
    labels = ["Safest Route", "Alternative 1", "Alternative 2"]
    for i, r in enumerate(results):
        r["label"] = labels[i] if i < len(labels) else f"Route {i+1}"

    return jsonify({
        "origin": {"name": origin_name, "lat": origin_lat, "lng": origin_lng},
        "destination": {"name": dest_name, "lat": dest_lat, "lng": dest_lng},
        "hour": hour,
        "profile": profile,
        "routes": results,
    })


# ── Report incident ───────────────────────────────────────────────────────────
@app.route("/api/report", methods=["POST"])
def report_incident():
    """
    Submit a user incident report.
    Body: {lat, lng, area, category, description, hour}
    """
    body = request.get_json(silent=True) or {}

    lat = body.get("lat")
    lng = body.get("lng")
    category = body.get("category", "other").strip()
    description = body.get("description", "").strip()
    area = body.get("area", "Delhi").strip()
    hour = int(body.get("hour", __import__("datetime").datetime.now().hour))

    if lat is None or lng is None:
        return jsonify({"error": "lat and lng required"}), 400
    if not description:
        return jsonify({"error": "description required"}), 400

    # Validate category
    from data.loader import CATEGORY_WEIGHTS
    if category not in CATEGORY_WEIGHTS:
        category = "other"

    save_user_report(
        lat=float(lat), lng=float(lng),
        area=area, category=category,
        description=description, hour=hour
    )

    return jsonify({"success": True, "message": "Report submitted. Thank you for keeping the community safe."})


# ── Dashboard stats ───────────────────────────────────────────────────────────
@app.route("/api/stats")
def stats():
    """Aggregated stats for the dashboard."""
    incidents = get_incidents()
    from data.loader import CATEGORY_WEIGHTS

    # Category breakdown
    cat_counts: dict = {}
    area_counts: dict = {}
    hour_counts = [0] * 24

    for inc in incidents:
        cat = CATEGORY_LABELS.get(inc.get("category", ""), "Other")
        reports = inc.get("reports", 1)
        cat_counts[cat] = cat_counts.get(cat, 0) + reports

        area = inc.get("area", "Unknown")
        area_counts[area] = area_counts.get(area, 0) + reports

        h = inc.get("hour", 20)
        if 0 <= h <= 23:
            hour_counts[h] += reports

    # Top 10 areas by incident count
    top_areas = sorted(area_counts.items(), key=lambda x: -x[1])[:10]

    # Safest areas (areas with data but low counts)
    all_areas = sorted(area_counts.items(), key=lambda x: x[1])
    safest = [a for a in all_areas if a[1] < 5][:5]

    return jsonify({
        "total_incidents": len(incidents),
        "total_reports": sum(i.get("reports", 1) for i in incidents),
        "category_breakdown": [{"name": k, "count": v} for k, v in
                                sorted(cat_counts.items(), key=lambda x: -x[1])],
        "top_hotspots": [{"area": a, "reports": c} for a, c in top_areas],
        "safest_areas": [{"area": a, "reports": c} for a, c in safest],
        "hourly_distribution": [{"hour": h, "reports": hour_counts[h]} for h in range(24)],
    })


# ── WhatsApp simulator ────────────────────────────────────────────────────────
@app.route("/api/whatsapp/simulate", methods=["POST"])
def whatsapp_simulate():
    """
    Simulate a WhatsApp bot interaction.
    Body: { "message": "Safe route from Lajpat Nagar to CP at 9pm" }
    Parses the message and returns a bot-style reply.
    """
    body = request.get_json(silent=True) or {}
    user_msg = body.get("message", "").strip()

    if not user_msg:
        return jsonify({"reply": "Hi! I'm SafeRoute. Send me a message like:\n'Safe route from Lajpat Nagar to Connaught Place at 9pm'"})

    # Simple NLP parsing using Groq
    parsed = _parse_whatsapp_message(user_msg)

    if not parsed or not parsed.get("origin") or not parsed.get("destination"):
        return jsonify({
            "reply": (
                "I didn't quite catch that. Try:\n"
                "'Route from [place] to [place] at [hour]pm'\n"
                "Example: 'Safe route from Karol Bagh to Saket at 8pm'"
            )
        })

    origin = parsed["origin"]
    destination = parsed["destination"]
    hour = parsed.get("hour", 20)

    # Geocode
    orig_geo = geocode(origin)
    dest_geo = geocode(destination)

    if not orig_geo or not dest_geo:
        return jsonify({"reply": f"Sorry, I couldn't find {origin or destination} in Delhi. Try a more specific location name."})

    # Get route
    routes = get_routes(orig_geo["lat"], orig_geo["lng"], dest_geo["lat"], dest_geo["lng"])
    if not routes:
        return jsonify({"reply": "Routing service unavailable right now. Please try again in a moment."})

    # Score best route
    best = routes[0]
    scored = score_route(best["coords"], hour)
    scored["distance_km"] = best["distance_km"]
    scored["duration_min"] = best["duration_min"]

    reply = get_ai_whatsapp_reply(user_msg, scored, origin, destination, hour)

    return jsonify({
        "reply": reply,
        "score": scored["score"],
        "tier": scored["tier"],
        "origin": orig_geo,
        "destination": dest_geo,
        "route_coords": best["coords"],
    })


def _parse_whatsapp_message(msg: str) -> dict:
    """Parse origin, destination, hour from a natural language message."""
    import re

    msg_lower = msg.lower()

    # Extract hour
    hour = 20  # default evening
    time_patterns = [
        (r'(\d{1,2})\s*am', lambda m: int(m.group(1)) % 12),
        (r'(\d{1,2})\s*pm', lambda m: int(m.group(1)) % 12 + 12),
        (r'at\s+(\d{1,2})', lambda m: int(m.group(1))),
        (r'(\d{1,2}):(\d{2})', lambda m: int(m.group(1))),
    ]
    for pattern, extractor in time_patterns:
        m = re.search(pattern, msg_lower)
        if m:
            hour = min(23, max(0, extractor(m)))
            break

    # Try Groq for parsing if available
    from ai_advisor import get_groq_key
    import requests as req
    key = get_groq_key()

    if key:
        try:
            prompt = f"""Extract origin and destination from this message: "{msg}"
Return ONLY valid JSON like: {{"origin": "place name", "destination": "place name"}}
If you cannot find both, return: {{"origin": null, "destination": null}}
No explanation, only JSON."""
            r = req.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json={"model": "llama3-8b-8192", "messages": [{"role": "user", "content": prompt}], "max_tokens": 60},
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=8,
            )
            import json
            data = r.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(data)
            parsed["hour"] = hour
            return parsed
        except Exception:
            pass

    # Fallback regex parsing
    from_match = re.search(r'from\s+([\w\s]+?)\s+to\s+([\w\s]+?)(?:\s+at|\s+around|$)', msg_lower)
    if from_match:
        return {
            "origin": from_match.group(1).strip().title(),
            "destination": from_match.group(2).strip().title(),
            "hour": hour,
        }

    return {"origin": None, "destination": None, "hour": hour}


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", 5001))
    print(f"\n  SafeRoute running at http://localhost:{port}")
    print(f"  Open your browser to http://localhost:{port}\n")
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(port=port, debug=debug, use_reloader=False)
