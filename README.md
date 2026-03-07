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
| Real Safecity dataset loading | done |
| Safety score engine | done |
| ORS route fetching (3 alternatives) | done |
| Route comparison map | done |
| Per-segment safety coloring | done |
| Incident heatmap overlay | done |
| Incident reporting (SQLite) | done |
| Groq AI safety advisories | done |
| Dashboard stats | done |
| WhatsApp bot simulator | done |

## Sprint 2 (next)
- [ ] WhatsApp webhook (real WhatsApp integration)
- [ ] NCRB district-level crime data layer
- [ ] User accounts + report history
- [ ] Email/WhatsApp waitlist integration

## Data Sources
- **Safecity dataset:** https://github.com/swkarlekar/safecity (MIT license)
- **NCRB crime data:** https://ncrb.gov.in/en/crime-in-india-table-addtional-table-and-chapter-contents
- **OpenRouteService:** https://openrouteservice.org (ODbL license)


