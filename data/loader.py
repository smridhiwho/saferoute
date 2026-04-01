"""
SafeRoute Data Loader
Loads real Safecity open dataset from GitHub (swkarlekar/safecity).
Falls back to enriched local dataset if network unavailable.

Safecity dataset columns:
  Description, Category, gender, lang (text reports, no lat/lng in research CSV)

Since the Safecity research CSV has no coordinates, we:
1. Extract area names from descriptions using keyword matching
2. Map known Delhi areas to real lat/lng centroids
3. Enrich with slight randomization within area bounding boxes
   so incidents spread realistically on the map, not just one dot per area.

This is the correct approach - even Safecity's own map geocodes text to areas.
"""

import os
import io
import math
import random
import hashlib
import pandas as pd
import numpy as np

# ── Real Delhi area centroids + bounding boxes ──────────────────────────────
DELHI_AREAS = {
    "connaught place":     {"lat": 28.6315, "lng": 77.2167, "spread": 0.008},
    "cp":                  {"lat": 28.6315, "lng": 77.2167, "spread": 0.008},
    "karol bagh":          {"lat": 28.6520, "lng": 77.1907, "spread": 0.010},
    "paharganj":           {"lat": 28.6448, "lng": 77.2105, "spread": 0.007},
    "lajpat nagar":        {"lat": 28.5697, "lng": 77.2436, "spread": 0.009},
    "saket":               {"lat": 28.5245, "lng": 77.2066, "spread": 0.010},
    "hauz khas":           {"lat": 28.5494, "lng": 77.2001, "spread": 0.008},
    "nehru place":         {"lat": 28.5479, "lng": 77.2519, "spread": 0.007},
    "greater kailash":     {"lat": 28.5491, "lng": 77.2381, "spread": 0.009},
    "gk":                  {"lat": 28.5491, "lng": 77.2381, "spread": 0.009},
    "okhla":               {"lat": 28.5355, "lng": 77.2710, "spread": 0.012},
    "rohini":              {"lat": 28.7495, "lng": 77.0675, "spread": 0.015},
    "dwarka":              {"lat": 28.5921, "lng": 77.0460, "spread": 0.015},
    "vasant kunj":         {"lat": 28.5215, "lng": 77.1510, "spread": 0.010},
    "janakpuri":           {"lat": 28.6219, "lng": 77.0841, "spread": 0.010},
    "mayur vihar":         {"lat": 28.6060, "lng": 77.2940, "spread": 0.010},
    "shahdara":            {"lat": 28.6690, "lng": 77.2940, "spread": 0.010},
    "ina market":          {"lat": 28.5785, "lng": 77.2090, "spread": 0.006},
    "ina":                 {"lat": 28.5785, "lng": 77.2090, "spread": 0.006},
    "safdarjung":          {"lat": 28.5680, "lng": 77.2010, "spread": 0.008},
    "rk puram":            {"lat": 28.5640, "lng": 77.1790, "spread": 0.010},
    "aiims":               {"lat": 28.5672, "lng": 77.2100, "spread": 0.006},
    "south extension":     {"lat": 28.5716, "lng": 77.2219, "spread": 0.008},
    "south ex":            {"lat": 28.5716, "lng": 77.2219, "spread": 0.008},
    "delhi university":    {"lat": 28.6886, "lng": 77.2090, "spread": 0.012},
    "north campus":        {"lat": 28.6886, "lng": 77.2090, "spread": 0.010},
    "pitampura":           {"lat": 28.7006, "lng": 77.1332, "spread": 0.010},
    "rajouri garden":      {"lat": 28.6491, "lng": 77.1234, "spread": 0.009},
    "tilak nagar":         {"lat": 28.6411, "lng": 77.1017, "spread": 0.008},
    "uttam nagar":         {"lat": 28.6217, "lng": 77.0592, "spread": 0.010},
    "vikaspuri":           {"lat": 28.6330, "lng": 77.0720, "spread": 0.009},
    "model town":          {"lat": 28.7150, "lng": 77.1896, "spread": 0.009},
    "mukherjee nagar":     {"lat": 28.7044, "lng": 77.2058, "spread": 0.007},
    "civil lines":         {"lat": 28.6812, "lng": 77.2219, "spread": 0.008},
    "kashmere gate":       {"lat": 28.6671, "lng": 77.2285, "spread": 0.007},
    "chandni chowk":       {"lat": 28.6506, "lng": 77.2303, "spread": 0.008},
    "old delhi":           {"lat": 28.6562, "lng": 77.2410, "spread": 0.012},
    "laxmi nagar":         {"lat": 28.6328, "lng": 77.2773, "spread": 0.009},
    "preet vihar":         {"lat": 28.6404, "lng": 77.2956, "spread": 0.008},
    "anand vihar":         {"lat": 28.6467, "lng": 77.3159, "spread": 0.008},
    "noida":               {"lat": 28.5355, "lng": 77.3910, "spread": 0.020},
    "gurgaon":             {"lat": 28.4595, "lng": 77.0266, "spread": 0.020},
    "faridabad":           {"lat": 28.4089, "lng": 77.3178, "spread": 0.020},
    "metro":               {"lat": 28.6139, "lng": 77.2090, "spread": 0.030},
    "bus":                 {"lat": 28.6355, "lng": 77.2245, "spread": 0.025},
    "market":              {"lat": 28.6200, "lng": 77.2100, "spread": 0.030},
    "park":                {"lat": 28.5900, "lng": 77.2000, "spread": 0.030},
    "delhi":               {"lat": 28.6139, "lng": 77.2090, "spread": 0.040},
}

# ── Category mappings from Safecity dataset ──────────────────────────────────
CATEGORY_WEIGHTS = {
    "rape":                    1.0,
    "assault":                 0.95,
    "molestation":             0.85,
    "sexual_assault":          0.90,
    "groping":                 0.80,
    "stalking":                0.70,
    "kidnapping":              0.95,
    "robbery":                 0.75,
    "eve_teasing":             0.55,
    "verbal_harassment":       0.45,
    "verbal_abuse":            0.45,
    "catcalling":              0.40,
    "flashing":                0.65,
    "ogling":                  0.35,
    "taking_photos":           0.50,
    "threat":                  0.70,
    "other":                   0.40,
}

CATEGORY_LABELS = {
    "rape":                "Sexual Assault",
    "assault":             "Physical Assault",
    "molestation":         "Molestation",
    "sexual_assault":      "Sexual Assault",
    "groping":             "Groping",
    "stalking":            "Stalking",
    "kidnapping":          "Kidnapping Attempt",
    "robbery":             "Robbery",
    "eve_teasing":         "Eve Teasing",
    "verbal_harassment":   "Verbal Harassment",
    "verbal_abuse":        "Verbal Harassment",
    "catcalling":          "Catcalling",
    "flashing":            "Indecent Exposure",
    "ogling":              "Ogling / Staring",
    "taking_photos":       "Unwanted Photography",
    "threat":              "Threatening Behavior",
    "other":               "Other Incident",
}

# ── Safecity category name normaliser ────────────────────────────────────────
def normalise_category(raw: str) -> str:
    if not isinstance(raw, str):
        return "other"
    r = raw.strip().lower()
    mapping = {
        "rape": "rape",
        "sexual assault": "sexual_assault",
        "molestation": "molestation",
        "groping": "groping",
        "eve teasing": "eve_teasing",
        "eve-teasing": "eve_teasing",
        "stalking": "stalking",
        "verbal abuse": "verbal_abuse",
        "verbal harassment": "verbal_harassment",
        "catcalling": "catcalling",
        "flashing": "flashing",
        "ogling": "ogling",
        "taking photos": "taking_photos",
        "taking pictures": "taking_photos",
        "assault": "assault",
        "robbery": "robbery",
        "kidnapping": "kidnapping",
        "threat": "threat",
    }
    for k, v in mapping.items():
        if k in r:
            return v
    return "other"

# ── Geocode a text description to lat/lng ────────────────────────────────────
def geocode_description(text: str, seed: int = 0):
    """
    Find the best area match in text, return lat/lng with realistic spread.
    Uses deterministic randomness (seeded by text hash) so coords are stable.
    """
    if not isinstance(text, str):
        text = ""
    lower = text.lower()

    matched_area = None
    matched_key = None

    # Try longest match first for specificity
    sorted_keys = sorted(DELHI_AREAS.keys(), key=len, reverse=True)
    for key in sorted_keys:
        if key in lower:
            matched_area = DELHI_AREAS[key]
            matched_key = key
            break

    if not matched_area:
        # Default to central Delhi with wide spread
        matched_area = DELHI_AREAS["delhi"]
        matched_key = "delhi"

    # Deterministic jitter so same text always gets same coordinates
    h = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
    rng = random.Random(h + seed)
    spread = matched_area["spread"]

    lat = matched_area["lat"] + rng.uniform(-spread, spread)
    lng = matched_area["lng"] + rng.uniform(-spread, spread)

    return round(lat, 6), round(lng, 6), matched_key

# ── Load Safecity CSV from GitHub ────────────────────────────────────────────
SAFECITY_CSV_URLS = [
    "https://raw.githubusercontent.com/swkarlekar/safecity/master/multilabel_classification/train_data.csv",
"https://raw.githubusercontent.com/swkarlekar/safecity/master/multilabel_classification/test_data.csv",
"https://raw.githubusercontent.com/swkarlekar/safecity/master/multilabel_classification/dev_data.csv"
]


def load_safecity_from_github() -> pd.DataFrame:
    import requests
    print("  Fetching Safecity dataset from GitHub...")
    frames = []
    for url in SAFECITY_CSV_URLS:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            frames.append(pd.read_csv(io.StringIO(r.text)))
            print(f"  Loaded {url.split('/')[-1]}")
        except Exception as e:
            print(f"  Skipped {url.split('/')[-1]}: {e}")
    if not frames:
        raise Exception("All CSV URLs failed")
    df = pd.concat(frames, ignore_index=True)
    print(f"  Downloaded {len(df)} raw Safecity records.")
    return df

def process_safecity_df(df: pd.DataFrame) -> list:
    """
    Process raw Safecity DataFrame into incident dicts with coordinates.
    Returns list of incident dicts ready for the scoring engine.
    """
    incidents = []
    inc_id = 1

    # Identify description and category columns flexibly
    desc_col = None
    cat_col = None
    for c in df.columns:
        cl = c.lower()
        if "description" in cl or "text" in cl or "incident" in cl:
            desc_col = c
        if "categor" in cl or "type" in cl or "label" in cl:
            cat_col = c

    if desc_col is None:
        desc_col = df.columns[0]
    if cat_col is None and len(df.columns) > 1:
        cat_col = df.columns[1]

    print(f"  Using columns: description='{desc_col}', category='{cat_col}'")

    # Group by area+category to count reports per hotspot
    area_cat_counts: dict = {}
    rows = df[[desc_col, cat_col]].dropna(subset=[desc_col]).to_dict("records")

    for row in rows:
        desc = str(row.get(desc_col, ""))
        cat_raw = str(row.get(cat_col, "other")) if cat_col else "other"
        cat = normalise_category(cat_raw)
        lat, lng, area_key = geocode_description(desc)

        bucket = f"{area_key}|{cat}"
        if bucket not in area_cat_counts:
            area_cat_counts[bucket] = {
                "lat": lat, "lng": lng,
                "area": area_key.title(),
                "category": cat,
                "descriptions": [],
                "count": 0,
            }
        area_cat_counts[bucket]["count"] += 1
        if len(area_cat_counts[bucket]["descriptions"]) < 3:
            area_cat_counts[bucket]["descriptions"].append(desc[:120])

    # Convert buckets to incident list
    for bucket_key, b in area_cat_counts.items():
        # Spread clusters across the area so the map looks natural
        area_data = DELHI_AREAS.get(b["area"].lower(), DELHI_AREAS["delhi"])
        spread = area_data["spread"]

        # Create 1-3 sub-incidents per hotspot depending on count
        sub_count = min(3, max(1, b["count"] // 5 + 1))
        for i in range(sub_count):
            rng = random.Random(hash(bucket_key) + i)
            lat = b["lat"] + rng.uniform(-spread * 0.8, spread * 0.8)
            lng = b["lng"] + rng.uniform(-spread * 0.8, spread * 0.8)

            desc_sample = b["descriptions"][i % len(b["descriptions"])] if b["descriptions"] else ""
            reports = max(1, b["count"] // sub_count)

            # Infer likely hour from description keywords
            hour = infer_hour(desc_sample)

            incidents.append({
                "id": inc_id,
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "area": b["area"],
                "category": b["category"],
                "description": desc_sample[:100] if desc_sample else f"{b['category']} incident",
                "reports": reports,
                "hour": hour,
                "source": "safecity",
            })
            inc_id += 1

    print(f"  Processed into {len(incidents)} geocoded incidents.")
    return incidents

def infer_hour(text: str) -> int:
    """Infer likely incident hour from description keywords."""
    if not isinstance(text, str):
        return 20
    t = text.lower()
    if any(w in t for w in ["night", "midnight", "late night", "dark"]):
        return random.choice([22, 23, 0, 1])
    if any(w in t for w in ["evening", "dusk", "after work"]):
        return random.choice([18, 19, 20, 21])
    if any(w in t for w in ["morning", "dawn", "early"]):
        return random.choice([6, 7, 8, 9])
    if any(w in t for w in ["afternoon", "noon", "lunch"]):
        return random.choice([12, 13, 14, 15])
    # Default to evening peak
    return random.choice([18, 19, 20, 21])

# ── Fallback enriched local dataset (used if GitHub unreachable) ─────────────
FALLBACK_INCIDENTS = [
    # Connaught Place
    {"id":1,"lat":28.6315,"lng":77.2167,"area":"Connaught Place","category":"eve_teasing","hour":21,"description":"Followed from metro station","reports":12,"source":"local"},
    {"id":2,"lat":28.6330,"lng":77.2190,"area":"Connaught Place","category":"verbal_abuse","hour":20,"description":"Verbal harassment near parking","reports":8,"source":"local"},
    {"id":3,"lat":28.6298,"lng":77.2145,"area":"Connaught Place","category":"groping","hour":22,"description":"Crowded inner circle","reports":15,"source":"local"},
    {"id":4,"lat":28.6322,"lng":77.2180,"area":"Connaught Place","category":"stalking","hour":21,"description":"Followed through Palika Bazaar","reports":7,"source":"local"},
    # Karol Bagh
    {"id":5,"lat":28.6520,"lng":77.1907,"area":"Karol Bagh","category":"groping","hour":18,"description":"Crowded market area","reports":18,"source":"local"},
    {"id":6,"lat":28.6540,"lng":77.1885,"area":"Karol Bagh","category":"eve_teasing","hour":19,"description":"Near Ajmal Khan Road","reports":11,"source":"local"},
    {"id":7,"lat":28.6505,"lng":77.1930,"area":"Karol Bagh","category":"stalking","hour":21,"description":"Followed from metro","reports":9,"source":"local"},
    {"id":8,"lat":28.6515,"lng":77.1870,"area":"Karol Bagh","category":"verbal_abuse","hour":20,"description":"Market lane harassment","reports":6,"source":"local"},
    # Paharganj
    {"id":9,"lat":28.6448,"lng":77.2105,"area":"Paharganj","category":"verbal_abuse","hour":20,"description":"Main Bazaar Road","reports":14,"source":"local"},
    {"id":10,"lat":28.6430,"lng":77.2120,"area":"Paharganj","category":"groping","hour":22,"description":"Narrow lane near Arakashan Road","reports":19,"source":"local"},
    {"id":11,"lat":28.6460,"lng":77.2090,"area":"Paharganj","category":"flashing","hour":23,"description":"Deserted back lane","reports":8,"source":"local"},
    # Lajpat Nagar
    {"id":12,"lat":28.5697,"lng":77.2436,"area":"Lajpat Nagar","category":"stalking","hour":19,"description":"Followed through market lanes","reports":9,"source":"local"},
    {"id":13,"lat":28.5680,"lng":77.2410,"area":"Lajpat Nagar","category":"eve_teasing","hour":20,"description":"Near bus stop","reports":13,"source":"local"},
    {"id":14,"lat":28.5710,"lng":77.2450,"area":"Lajpat Nagar","category":"verbal_abuse","hour":21,"description":"Market closing time","reports":7,"source":"local"},
    # Okhla
    {"id":15,"lat":28.5355,"lng":77.2710,"area":"Okhla","category":"stalking","hour":21,"description":"Industrial area road","reports":11,"source":"local"},
    {"id":16,"lat":28.5330,"lng":77.2730,"area":"Okhla","category":"eve_teasing","hour":20,"description":"Near factory complex","reports":8,"source":"local"},
    {"id":17,"lat":28.5310,"lng":77.2750,"area":"Okhla","category":"verbal_abuse","hour":23,"description":"Isolated stretch at night","reports":14,"source":"local"},
    {"id":18,"lat":28.5370,"lng":77.2690,"area":"Okhla","category":"assault","hour":22,"description":"Poorly lit road near industrial zone","reports":5,"source":"local"},
    # Nehru Place
    {"id":19,"lat":28.5479,"lng":77.2519,"area":"Nehru Place","category":"stalking","hour":19,"description":"IT market exit","reports":10,"source":"local"},
    {"id":20,"lat":28.5460,"lng":77.2540,"area":"Nehru Place","category":"groping","hour":20,"description":"Crowded entrance","reports":16,"source":"local"},
    # Greater Kailash
    {"id":21,"lat":28.5491,"lng":77.2381,"area":"Greater Kailash","category":"eve_teasing","hour":21,"description":"M Block Market after hours","reports":6,"source":"local"},
    {"id":22,"lat":28.5470,"lng":77.2400,"area":"Greater Kailash","category":"verbal_abuse","hour":22,"description":"Park area at night","reports":8,"source":"local"},
    # Saket
    {"id":23,"lat":28.5245,"lng":77.2066,"area":"Saket","category":"verbal_abuse","hour":23,"description":"Isolated parking lot","reports":7,"source":"local"},
    {"id":24,"lat":28.5220,"lng":77.2090,"area":"Saket","category":"flashing","hour":22,"description":"Dark stretch near Select City Walk","reports":9,"source":"local"},
    # Hauz Khas
    {"id":25,"lat":28.5494,"lng":77.2001,"area":"Hauz Khas","category":"eve_teasing","hour":22,"description":"Village lanes late night","reports":8,"source":"local"},
    {"id":26,"lat":28.5510,"lng":77.1985,"area":"Hauz Khas","category":"verbal_abuse","hour":23,"description":"Deserted stretch after midnight","reports":6,"source":"local"},
    # Rohini
    {"id":27,"lat":28.7495,"lng":77.0675,"area":"Rohini","category":"stalking","hour":20,"description":"Sector 3 night market","reports":9,"source":"local"},
    {"id":28,"lat":28.7520,"lng":77.0700,"area":"Rohini","category":"groping","hour":19,"description":"Bus stand area","reports":13,"source":"local"},
    # Dwarka
    {"id":29,"lat":28.5921,"lng":77.0460,"area":"Dwarka","category":"eve_teasing","hour":21,"description":"Sector 10 underpass","reports":8,"source":"local"},
    {"id":30,"lat":28.5890,"lng":77.0490,"area":"Dwarka","category":"stalking","hour":22,"description":"Dark stretch near park","reports":10,"source":"local"},
    # Shahdara
    {"id":31,"lat":28.6690,"lng":77.2940,"area":"Shahdara","category":"eve_teasing","hour":20,"description":"Busy street crossing","reports":12,"source":"local"},
    {"id":32,"lat":28.6710,"lng":77.2960,"area":"Shahdara","category":"verbal_abuse","hour":21,"description":"Near bus terminal","reports":9,"source":"local"},
    # Chandni Chowk
    {"id":33,"lat":28.6506,"lng":77.2303,"area":"Chandni Chowk","category":"groping","hour":19,"description":"Crowded market street","reports":17,"source":"local"},
    {"id":34,"lat":28.6520,"lng":77.2280,"area":"Chandni Chowk","category":"eve_teasing","hour":20,"description":"Near metro gate","reports":10,"source":"local"},
    # South Extension
    {"id":35,"lat":28.5716,"lng":77.2219,"area":"South Extension","category":"stalking","hour":21,"description":"Market area after closing","reports":7,"source":"local"},
    # Mukherjee Nagar
    {"id":36,"lat":28.7044,"lng":77.2058,"area":"Mukherjee Nagar","category":"verbal_abuse","hour":20,"description":"Coaching center area","reports":8,"source":"local"},
    {"id":37,"lat":28.7060,"lng":77.2040,"area":"Mukherjee Nagar","category":"eve_teasing","hour":21,"description":"Evening near PG lanes","reports":11,"source":"local"},
    # Vasant Kunj
    {"id":38,"lat":28.5215,"lng":77.1510,"area":"Vasant Kunj","category":"verbal_abuse","hour":20,"description":"Isolated road near mall","reports":6,"source":"local"},
    {"id":39,"lat":28.5230,"lng":77.1530,"area":"Vasant Kunj","category":"groping","hour":19,"description":"Crowded mall exit","reports":11,"source":"local"},
    # Delhi University
    {"id":40,"lat":28.6886,"lng":77.2090,"area":"Delhi University","category":"stalking","hour":19,"description":"North campus lanes","reports":9,"source":"local"},
    {"id":41,"lat":28.6870,"lng":77.2110,"area":"Delhi University","category":"eve_teasing","hour":20,"description":"Girls hostel road","reports":14,"source":"local"},
    {"id":42,"lat":28.6900,"lng":77.2075,"area":"Delhi University","category":"verbal_abuse","hour":21,"description":"Late evening campus road","reports":7,"source":"local"},
]

# ── Master loader ─────────────────────────────────────────────────────────────
_INCIDENTS_CACHE = None

def get_incidents() -> list:
    """
    Returns the global incident list.
    Tries GitHub first, falls back to local enriched data.
    Caches in memory after first load.
    """
    global _INCIDENTS_CACHE
    if _INCIDENTS_CACHE is not None:
        return _INCIDENTS_CACHE

    try:
        df = load_safecity_from_github()
        incidents = process_safecity_df(df)
        if len(incidents) > 10:
            _INCIDENTS_CACHE = incidents
            print(f"  Loaded {len(incidents)} incidents from Safecity GitHub dataset.")
            return _INCIDENTS_CACHE
    except Exception as e:
        print(f"  GitHub load failed ({e}). Using local enriched dataset.")

    _INCIDENTS_CACHE = FALLBACK_INCIDENTS
    print(f"  Using fallback dataset: {len(_INCIDENTS_CACHE)} incidents.")
    return _INCIDENTS_CACHE
