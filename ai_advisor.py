"""
Groq AI integration for SafeRoute.

Uses llama3-8b-8192 (free, fast) to generate natural-language
safety advice based on route score and incident breakdown.
"""

import logging
import os
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama3-8b-8192"

# Module-level session — reuses TCP connections across all Groq calls
_session = requests.Session()


# ---------------------------------------------------------------------------
# Key helper
# ---------------------------------------------------------------------------

def get_groq_key() -> Optional[str]:
    """Return the Groq API key, or None if unset / still a placeholder."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return key if key and not key.startswith("your_") else None


# ---------------------------------------------------------------------------
# Shared private HTTP helper — fixes the DRY violation
# ---------------------------------------------------------------------------

def _call_groq(prompt: str, max_tokens: int = 180, temperature: float = 0.6) -> Optional[str]:
    """
    Send a single-turn prompt to Groq and return the text response.
    Returns None on any failure so callers can fall back gracefully.
    """
    key = get_groq_key()
    if not key:
        return None
    try:
        r = _session.post(
            GROQ_API_URL,
            json={
                "model":       GROQ_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  max_tokens,
                "temperature": temperature,
            },
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except requests.RequestException:
        logger.exception("Groq API call failed")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ai_safety_advice(
    route_result: Dict, origin: str, destination: str, hour: int
) -> str:
    """
    Generate a warm, practical safety advisory for the chosen route.
    Falls back to a rule-based message if Groq is unavailable.
    """
    score      = route_result.get("score", 50)
    tier       = route_result.get("tier", "caution")
    alerts     = route_result.get("alerts", [])
    categories = route_result.get("category_breakdown", {})
    time_note  = route_result.get("time_note", "")

    alert_summary = "; ".join(
        f"{a['category_label']} near {a['area']} ({a['distance_m']}m away)"
        for a in alerts[:3]
    ) or "none significant"

    cat_summary = ", ".join(
        f"{k} ({v} reports)" for k, v in list(categories.items())[:4]
    ) or "minimal reported incidents"

    prompt = f"""You are SafeRoute's AI advisor. A woman in Delhi is planning to travel.

Route: {origin} to {destination}
Time: {hour:02d}:00 hours
Safety score: {score}/100 ({tier})
Nearby incident types: {cat_summary}
Top alerts: {alert_summary}
Time context: {time_note}

Write a SHORT (3-4 sentences), warm, practical safety advisory for this route.
- Be empowering, not scary.
- Give 1-2 concrete actionable tips specific to what was found.
- If score is high (>70), be reassuring but not careless.
- If score is low (<45), be direct about risks and suggest specific precautions.
- Do NOT use em dashes (--) or (—). Use commas or periods instead.
- Speak directly to the woman, use "you".
- Do not repeat the score number.
"""

    reply = _call_groq(prompt, max_tokens=180, temperature=0.6)
    return reply if reply is not None else _fallback_advice(route_result)


def get_ai_whatsapp_reply(
    user_message: str, route_result: Dict,
    origin: str, destination: str, hour: int,
) -> str:
    """
    Generate a WhatsApp-style short reply with route safety info.
    Falls back to a formatted rule-based reply if Groq is unavailable.
    """
    score    = route_result.get("score", 50)
    tier     = route_result.get("tier_label", "Use Caution")
    alerts   = route_result.get("alerts", [])
    duration = route_result.get("duration_min", "?")
    distance = route_result.get("distance_km", "?")

    top_alerts = "\n".join(
        f"- {a['category_label']} reported near {a['area']}"
        for a in alerts[:3]
    ) or "- No major incidents flagged on this route"

    prompt = f"""You are SafeRoute, a WhatsApp safety bot for women in Delhi.

User asked: "{user_message}"
Route: {origin} to {destination} at {hour:02d}:00
Safety score: {score}/100 ({tier})
Distance: {distance} km, approx {duration} min walk
Top concerns:
{top_alerts}

Write a WhatsApp reply (max 5 lines). Use emoji sparingly (1-2 total).
Format:
  Line 1: Short safety verdict with score emoji indicator
  Line 2: Distance and time
  Line 3: One key thing to know
  Line 4: One practical tip
  Line 5: Encouraging sign-off
Do NOT use em dashes. Be warm, concise, like a trusted friend texting back.
"""

    reply = _call_groq(prompt, max_tokens=160, temperature=0.65)
    return reply if reply is not None else _fallback_whatsapp(route_result, origin, destination)


# ---------------------------------------------------------------------------
# Fallbacks — used when Groq is unavailable
# ---------------------------------------------------------------------------

def _fallback_advice(result: Dict) -> str:
    tier = result.get("tier", "caution")
    if tier == "safe":
        return (
            "This route has relatively low reported incident activity. "
            "Stay on well-lit main roads and keep your phone charged. "
            "Trust your instincts and move with purpose."
        )
    if tier == "caution":
        return (
            "This route passes through areas with some reported incidents. "
            "Stick to busy, well-lit streets and avoid isolated lanes. "
            "Consider sharing your live location with a trusted contact. "
            "If possible, travel during peak hours when foot traffic is higher."
        )
    return (
        "This route has significant incident reports in our data. "
        "We strongly recommend considering an alternate path if available. "
        "If you must use this route, travel with someone else, stay on main roads, "
        "and share your live location before you set out."
    )


def _fallback_whatsapp(result: Dict, origin: str, destination: str) -> str:
    score    = result.get("score", 50)
    tier     = result.get("tier_label", "Use Caution")
    duration = result.get("duration_min", "?")
    distance = result.get("distance_km", "?")
    emoji    = "✅" if score >= 70 else ("⚠️" if score >= 45 else "🔴")
    return (
        f"{emoji} {origin} to {destination}: Safety score {score}/100 ({tier})\n"
        f"Distance: {distance} km, approx {duration} min\n"
        f"{result.get('time_note', '')}\n"
        f"{result.get('advice', 'Stay alert and trust your instincts.')}\n"
        f"Stay safe. You got this."
    )