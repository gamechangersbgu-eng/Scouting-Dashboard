# Scouting Dashboard

A local scouting tool for Israeli youth football (IFA / football.org.il). It has two parts:

- `ifa_scraper/` scrapes the IFA site and writes CSVs into `data/`.
- `dashboard/` is a Flask app that reads those CSVs into memory at startup and serves a
  single-page UI (Hebrew, RTL) for searching players and viewing their careers.

The dashboard needs no network access and no database (except OpenStreetMap map tiles in
the browser).

## Running the dashboard

The virtual environment is in `.venv` (Python 3.10, Flask, pandas).

```powershell
.\.venv\Scripts\python.exe -m dashboard.app            # opens http://127.0.0.1:5000/
.\.venv\Scripts\python.exe -m dashboard.app --no-browser --port 5001
```

Always run `dashboard.app`, not `dashboard.app_core`: `app.py` patches the league and
location/radius filters onto `ScoutingData.search` and `summary`.

## Layout

- `dashboard/app_core.py` – `ScoutingData` (loads CSVs, search, player detail, career
  track, map venues, likely-origin inference) and the Flask routes:
  `/api/summary`, `/api/players`, `/api/player/<id>`.
- `dashboard/app.py` – entry point; adds league and location-radius filters.
- `dashboard/static/` – `index.html`, `app.js` (UI rendering, map), `league-filter.js`
  (adds `league` to `/api/players` requests), `styles.css`, Leaflet.
- `data/` – `players_youth.csv` (one aggregated row per player),
  `player_season_stats.csv` (one row per player × season × team),
  `player_history.csv` (optional, older seasons without stats), `player_details.csv`,
  `team_locations.csv`, `team_fields.csv`.
- `ifa_scraper/config.py` – seasons, leagues, age-group ladder.

Note: the repo root contains stray duplicate copies of files (`app.js`, `parse (7).py`,
`README.md`, `requirements.txt`, etc.) whose contents don't match their names. The real
code is under `dashboard/` and `ifa_scraper/`.

## Rules

- New dashboard metrics and calculations are computed from `player_season_stats.csv`,
  not from `players_youth.csv`, so it is easy to switch to a normalized schema later.
- Do not run the scraper (`ifa_scraper`) without asking first.
- Do not `git push` without asking first.
- Use the virtual environment in `.venv`.
