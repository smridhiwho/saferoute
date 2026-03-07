# SafeRoute MVP

AI-powered women's safety routing for Delhi. Built for the UNICEF AI Ventures Accelerator 2026.

## Stack
- **Backend:** Python + Flask (monolith, MVP stage)
- **AI:** Groq LLaMA3 (free) for safety advisories + WhatsApp NLP
- **Routing:** OpenRouteService (free, open source)
- **Data:** Safecity open dataset (GitHub) + NCRB crime data
- **Frontend:** Vanilla JS + Leaflet.js
- **DB:** SQLite for user-submitted incident reports

## Setup

### 1. Get free API keys
- **ORS (routing):** https://openrouteservice.org/dev/#/signup
- **Groq (AI):** https://console.groq.com

Both are completely free. No credit card needed.

### 2. Configure
```bash
cp .env.example .env
# Edit .env and add your keys
```

### 3. Install & run
```bash
pip install -r requirements.txt
python start.py
```
Open http://localhost:5001

## Features (Sprint 1)

| Feature | Status |
|---------|--------|
| Real Safecity dataset loading | pending |
| Safety score engine | pending |
| ORS route fetching (3 alternatives) | pending |
| Route comparison map | pending |
| Per-segment safety coloring | pending |
| Incident heatmap overlay | pending |
| Incident reporting (SQLite) | pending |
| Groq AI safety advisories | pending |
| Dashboard stats | pending |
| WhatsApp bot simulator | pending |

## Sprint 2 (next)
- [ ] Twilio WhatsApp webhook (real WhatsApp integration)
- [ ] NCRB district-level crime data layer
- [ ] User accounts + report history
- [ ] Email/WhatsApp waitlist integration

## Data Sources
- **Safecity dataset:** https://github.com/swkarlekar/safecity (MIT license)
- **NCRB crime data:** https://ncrb.gov.in/en/crime-in-india-table-addtional-table-and-chapter-contents
- **OpenRouteService:** https://openrouteservice.org (ODbL license)


