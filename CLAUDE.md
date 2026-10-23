# Scouting Dashboard

A local scouting tool for Israeli youth football (IFA / football.org.il). It has two parts:

- `ifa_scraper/` scrapes the IFA site and writes CSVs into `data/`.
- `dashboard/` is a Flask app that reads the club's normalised schema in `data/db/` into
  memory at startup and serves a single-page UI (Hebrew, RTL) for searching players and
  viewing their careers.

The dashboard needs no network access and no database server (except OpenStreetMap map
tiles in the browser).

## Running the dashboard

The virtual environment is in `.venv` (Python 3.10, Flask, pandas).

```powershell
.\.venv\Scripts\python.exe -m dashboard.app            # opens http://127.0.0.1:5000/
.\.venv\Scripts\python.exe -m dashboard.app --no-browser --port 5001
```

Always run `dashboard.app`, not `dashboard.app_core`: `app.py` patches the league and
location/radius filters onto `ScoutingData.search` and `summary`.

Data is read once at startup, so restart the dashboard after `data/db/` changes.

## Data: two separate sets of files

- **`data/db/` — what the dashboard reads.** An export of the club's SQL Server schema,
  one CSV per table. Kept in Git, except `Favorite_Players.csv`.
- **`data/` (top level) — what the scraper writes.** `players_youth.csv`,
  `player_season_stats.csv`, `player_details.csv`, `team_locations.csv`, ... The dashboard
  no longer reads these. **Do not delete them**: the scraper still writes there.

Until the scraper is changed to write directly into `data/db/`, running it does **not**
update the dashboard. Changing the scraper is the responsibility of whoever maintains it.

### Schema tables (`data/db/`)

| Table | Key | Contents |
|---|---|---|
| `Players` | `player_id` | `player_name`, `birth_year`, `birth_month`, `image_url`. IFA publishes month and year of birth only, never a day, so there is no `birth_date`. |
| `Teams` | `team_id` | `team_name`, `venue_id` |
| `Venues` | `venue_id` | `venue_name`, `field_address`, `field_city`, `latitude`, `longitude`, `geocode_precision` (`street` / `locality` / `fallback`; used for the likely-origin confidence) |
| `Seasons` | `season_id` | `season_name` (e.g. `2025/26`), `season_end_year` |
| `Competitions` | `competition_id` | `competition_name`, `age_group_id`, `region`. Ids are the schema's own (1..n), **not** IFA league ids. |
| `Age_Groups` | `age_group_id` | `age_group_name`, `max_age` (the dashboard orders brackets by `max_age`) |
| `Player_Season_Stats` | `stat_id` | one row per player × season × team (+ `competition_id`), with the season statistics |
| `Player_Match_Stats` | `match_stat_id` | per-match rows for "recent matches"; filled by the weekly update. Its `competition_id` / `team_id` refer to `Competitions` / `Teams`. |
| `Favorite_Players` | `player_id` | `created_at`, `notes`; written by the dashboard. Not in Git (club data). |

`created_at` / `updated_at` in the exported tables were mangled when the files were opened
in Excel (only `mm:ss.f` survived). The dashboard ignores them; they will be fixed by a
fresh export. **Do not open and save `data/db/` files in Excel** — it breaks dates again
(and `Favorite_Players.csv` relies on its `created_at`).

## Code layout

- `dashboard/db.py` – `SchemaData`: reads the `data/db/` tables, joins them, and derives what
  the scraper used to precompute (current team, totals, leagues, playing above age) from
  `Player_Season_Stats`, using the same rules as `ifa_scraper/run.py`.
- `dashboard/app_core.py` – `ScoutingData` (search, player detail, season metrics, career
  track, map venues, likely-origin inference, favourites store) and the Flask routes:
  `/api/summary`, `/api/players`, `/api/player/<id>`, `/api/player/<id>/matches`,
  `/api/favorites` (GET), `/api/favorites/<id>` (PUT / DELETE).
- `dashboard/app.py` – entry point; adds league and location-radius filters.
- `dashboard/static/` – `index.html`, `app.js` (UI rendering, map), `league-filter.js`
  (adds `league` to `/api/players` requests), `styles.css`, Leaflet.
- `ifa_scraper/config.py` – seasons, leagues, the youth age-group ladder.

Note: the repo root contains stray duplicate copies of files (`app.js`, `parse (7).py`,
`README.md`, `requirements.txt`, etc.) whose contents don't match their names. The real
code is under `dashboard/` and `ifa_scraper/`.

## History in the schema

The club wants earlier seasons too (mainly where a player played in their first season).
There is **no separate history table**. History goes into the existing tables:

- old seasons as rows in `Seasons`;
- kids leagues as rows in `Competitions` and `Age_Groups`;
- the appearances as rows in `Player_Season_Stats`, with the statistics columns empty
  when there is no data.

The dashboard must keep working with any number of seasons and with empty columns:

- A `Player_Season_Stats` row whose statistics are **all** empty is a history row: it shows
  team, season and age group, with "—" instead of numbers. It never counts as zero games.
- A row with only **some** columns empty shows those as "—" (None), never as 0. Rows without
  `minutes` are left out of the season metrics.
- "Playing above age" is computed **only for the youth brackets** (נערים ג' up to נוער),
  never for kids leagues: moving from ילדים into נערים is normal ageing, not playing up.
- `MATCH_MINUTES` in `app_core.py` (match length per bracket, used by the per-match tile
  metrics) has no entry for kids brackets yet. That is harmless while kids rows carry no
  statistics; add their match lengths before kids statistics are imported.

## Rules

- New dashboard metrics and calculations are computed from `Player_Season_Stats` (in
  `data/db/`), not from aggregates such as the old `players_youth.csv`.
- Do not run the scraper (`ifa_scraper`) without asking first.
- Do not `git push` without asking first.
- Do not delete the scraper's files in `data/`.
- Use the virtual environment in `.venv`.
