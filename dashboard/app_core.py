"""Local scouting dashboard over the scraped youth-league data.

Serves a single page plus a small JSON API. Everything is read from the CSVs in data/
once at startup and held in memory, so the dashboard needs no network access and no
database. Run it with:

    python -m dashboard.app
"""

import argparse
import logging
import math
import webbrowser
from collections import defaultdict

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from ifa_scraper import config, run

log = logging.getLogger(__name__)

STATIC_DIR = config.PROJECT_ROOT / "dashboard" / "static"

TOTAL_FIELDS = [
    "goals_total",
    "goals_league",
    "goals_cup",
    "games_total",
    "minutes_total",
    "avg_minutes_per_game",
    "starts",
    "sub_on",
    "sub_off",
    "yellow_cards_total",
    "red_cards",
]

SEASON_STAT_FIELDS = [
    "games",
    "goals",
    "minutes",
    "starts",
    "sub_on",
    "sub_off",
    "yellow_cards_league_cup",
    "yellow_cards_toto",
    "red_cards",
]


def _clean(value):
    """Make a pandas value safe for JSON: NaN/NaT become None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if pd.isna(value):
        return None
    return value


def _text(value):
    """Read a string column as a string, treating a missing value as empty.

    pandas reads an empty CSV cell as NaN, which is truthy, so `value or ""` keeps the
    NaN and it serialises as a bare `NaN` literal that JSON.parse rejects in the
    browser. Every text field therefore goes through here.
    """
    cleaned = _clean(value)
    return "" if cleaned is None else str(cleaned)


class ScoutingData:
    """In-memory view of the scraped CSVs, indexed for per-player lookups."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or config.DATA_DIR
        self.players = self._load_players()
        self.season_rows = self._load_season_rows()
        self.locations = self._load_locations()
        self.details = self._load_details()

        self.rows_by_player = defaultdict(list)
        for row in self.season_rows:
            self.rows_by_player[row["player_id"]].append(row)

        # Recomputed here rather than read from the CSV because players_youth.csv only
        # carries the aggregate flag, while the season table shows a flag per spell.
        self.natural_brackets = run.natural_age_groups(self.season_rows, self.details)

        self.search_index = [
            {
                "player_id": p["player_id"],
                "player_name": p["player_name"],
                "birth_year": _clean(p.get("birth_year")),
                "current_team": _text(p.get("current_team")),
                "plays_above_age": p.get("plays_above_age") == "Yes",
                "age_groups_above": int(p.get("age_groups_above") or 0),
                "goals_total": int(p.get("goals_total") or 0),
                "games_total": int(p.get("games_total") or 0),
                "minutes_total": int(p.get("minutes_total") or 0),
                "haystack": f"{p['player_name']} {p['player_id']} {_text(p.get('current_team'))}",
            }
            for p in self.players.values()
        ]

    def _load_players(self):
        path = self.data_dir / "players_youth.csv"
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"player_id": str})
        frame["birth_year"] = pd.to_numeric(frame["birth_year"], errors="coerce").astype(
            "Int64"
        )
        log.info("loaded %s players", len(frame))
        return {row["player_id"]: row for row in frame.to_dict("records")}

    def _load_season_rows(self):
        path = self.data_dir / "player_season_stats.csv"
        frame = pd.read_csv(
            path, encoding="utf-8-sig", dtype={"player_id": str, "team_id": str}
        )
        log.info("loaded %s player-season rows", len(frame))
        return frame.to_dict("records")

    def _load_locations(self):
        path = self.data_dir / "team_locations.csv"
        if not path.exists():
            log.warning("%s missing; the map will be empty", path.name)
            return {}
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"team_id": str})
        return {row["team_id"]: row for row in frame.to_dict("records")}

    def _load_details(self):
        path = self.data_dir / "player_details.csv"
        if not path.exists():
            return {}
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"player_id": str})
        details = {}
        for row in frame.to_dict("records"):
            year = _clean(row.get("birth_year"))
            details[row["player_id"]] = {
                "birth_year": int(year) if year is not None else None,
            }
        return details

    def search(
        self,
        query,
        above_age_only=False,
        birth_year=None,
        current_team=None,
        limit=40,
    ):
        """Rank players by how well they match a free-text query.

        Names are published surname-first, so a scout may type either part; matching on
        a combined haystack of name, id and current club covers both without needing
        the query terms in any particular order.
        """
        terms = [t for t in (query or "").split() if t]
        results = []
        for entry in self.search_index:
            if above_age_only and not entry["plays_above_age"]:
                continue
            if birth_year and entry["birth_year"] != birth_year:
                continue
            if current_team and entry["current_team"] != current_team:
                continue
            if terms and not all(term in entry["haystack"] for term in terms):
                continue
            results.append(entry)

        if terms:
            # Prefer names that start with the query over mid-string matches.
            first = terms[0]
            results.sort(
                key=lambda e: (
                    not e["player_name"].startswith(first),
                    -e["minutes_total"],
                )
            )
        else:
            results.sort(key=lambda e: (-e["goals_total"], -e["minutes_total"]))

        return [{k: v for k, v in e.items() if k != "haystack"} for e in results[:limit]]

    def player(self, player_id):
        """Full detail for one player: totals, per-season spells and located teams."""
        player = self.players.get(player_id)
        if player is None:
            return None

        rows = sorted(
            self.rows_by_player.get(player_id, []),
            key=lambda r: (r["season_id"], r["team_name"]),
        )

        seasons = []
        for row in rows:
            steps = run.age_groups_above(row, self.details, self.natural_brackets)
            seasons.append(
                {
                    "season": row["season"],
                    "season_id": int(row["season_id"]),
                    "team_id": row["team_id"],
                    "team_name": _text(row["team_name"]),
                    "age_group": _text(row["age_group"]),
                    "league_name": _text(row["league_name"]),
                    "above_age_steps": steps if steps and steps > 0 else 0,
                    **{field: int(row.get(field) or 0) for field in SEASON_STAT_FIELDS},
                }
            )

        teams, venues = self._career_track(seasons)

        return {
            "player_id": player_id,
            "player_name": player["player_name"],
            "birth_year": _clean(player.get("birth_year")),
            "image_url": _text(player.get("image_url")) or None,
            "current_team": _text(player.get("current_team")),
            "num_teams": int(player.get("num_teams") or 0),
            "age_groups": _text(player.get("age_groups")),
            "leagues": _text(player.get("leagues")),
            "seasons_played": _text(player.get("seasons")),
            "plays_above_age": player.get("plays_above_age") == "Yes",
            "age_groups_above": int(player.get("age_groups_above") or 0),
            "above_age_history": _text(player.get("above_age_history")),
            "totals": {f: _clean(player.get(f)) for f in TOTAL_FIELDS},
            "seasons": seasons[::-1],
            "teams": teams,
            "venues": venues,
        }

    def _career_track(self, seasons):
        """Build the club spells and the map markers for one player.

        Returns (teams, venues): one entry per squad the player turned out for, and one
        per distinct place. Both are ordered earliest first, since that order is what
        drives marker size in the UI.
        """
        spells = {}
        for entry in seasons:
            team_id = entry["team_id"]
            spell = spells.get(team_id)
            if spell is None:
                spells[team_id] = {
                    "team_id": team_id,
                    "team_name": entry["team_name"],
                    "first_season_id": entry["season_id"],
                    "last_season_id": entry["season_id"],
                    "seasons": [entry["season"]],
                    "age_groups": [entry["age_group"]],
                    "games": entry["games"],
                    "goals": entry["goals"],
                    "minutes": entry["minutes"],
                    "above_age_steps": entry["above_age_steps"],
                }
                continue
            spell["first_season_id"] = min(spell["first_season_id"], entry["season_id"])
            spell["last_season_id"] = max(spell["last_season_id"], entry["season_id"])
            if entry["season"] not in spell["seasons"]:
                spell["seasons"].append(entry["season"])
            if entry["age_group"] not in spell["age_groups"]:
                spell["age_groups"].append(entry["age_group"])
            spell["games"] += entry["games"]
            spell["goals"] += entry["goals"]
            spell["minutes"] += entry["minutes"]
            spell["above_age_steps"] = max(
                spell["above_age_steps"], entry["above_age_steps"]
            )

        track = sorted(spells.values(), key=lambda s: s["first_season_id"])
        latest = max((s["last_season_id"] for s in track), default=None)

        for order, spell in enumerate(track):
            location = self.locations.get(spell["team_id"]) or {}
            lat, lon = _clean(location.get("lat")), _clean(location.get("lon"))
            spell["order"] = order
            spell["is_current"] = spell["last_season_id"] == latest
            spell["lat"] = float(lat) if lat is not None else None
            spell["lon"] = float(lon) if lon is not None else None
            spell["field_name"] = _text(location.get("field_name"))
            spell["city"] = _text(location.get("city"))
            spell["address"] = _text(location.get("address"))
            spell["precision"] = _text(location.get("precision")) or "unresolved"

        venues = self._venues(track)
        for spell in track:
            spell["seasons"] = ", ".join(spell["seasons"])
            spell["age_groups"] = ", ".join(spell["age_groups"])
        return track, venues

    def _venues(self, track):
        """Collapse club spells that share a location into one map marker.

        A club enters a separate squad per age bracket, each with its own team id, so a
        player who moves up a bracket at the same club would otherwise stack several
        identical markers on one point. Spells at the same coordinates are therefore
        merged, and the earliest season among them sets the marker size.
        """
        venues = {}
        for spell in track:
            if spell["lat"] is None or spell["lon"] is None:
                continue
            key = (round(spell["lat"], 4), round(spell["lon"], 4))
            venue = venues.get(key)
            if venue is None:
                venues[key] = {
                    "lat": spell["lat"],
                    "lon": spell["lon"],
                    "city": spell["city"],
                    "field_name": spell["field_name"],
                    "teams": [spell["team_name"]],
                    "seasons": list(spell["seasons"]),
                    "first_season_id": spell["first_season_id"],
                    "last_season_id": spell["last_season_id"],
                    "games": spell["games"],
                    "goals": spell["goals"],
                    "minutes": spell["minutes"],
                    "above_age_steps": spell["above_age_steps"],
                    "is_current": spell["is_current"],
                }
                continue
            if spell["team_name"] not in venue["teams"]:
                venue["teams"].append(spell["team_name"])
            for season in spell["seasons"]:
                if season not in venue["seasons"]:
                    venue["seasons"].append(season)
            venue["first_season_id"] = min(
                venue["first_season_id"], spell["first_season_id"]
            )
            venue["last_season_id"] = max(
                venue["last_season_id"], spell["last_season_id"]
            )
            venue["games"] += spell["games"]
            venue["goals"] += spell["goals"]
            venue["minutes"] += spell["minutes"]
            venue["above_age_steps"] = max(
                venue["above_age_steps"], spell["above_age_steps"]
            )
            venue["is_current"] = venue["is_current"] or spell["is_current"]

        ordered = sorted(venues.values(), key=lambda v: v["first_season_id"])
        for order, venue in enumerate(ordered):
            venue["order"] = order
            venue["seasons"] = ", ".join(sorted(venue["seasons"]))
        return ordered

    def summary(self):
        located = sum(1 for row in self.locations.values() if _clean(row.get("lat")))
        return {
            "players": len(self.players),
            "player_seasons": len(self.season_rows),
            "teams": len(self.locations),
            "teams_located": located,
            "above_age": sum(1 for e in self.search_index if e["plays_above_age"]),
            "seasons": sorted(
                {row["season"] for row in self.season_rows}, reverse=True
            ),
            "birth_years": sorted(
                {e["birth_year"] for e in self.search_index if e["birth_year"]},
                reverse=True,
            ),
            # Only clubs that somebody is currently at, since that is what the filter
            # matches on; offering the rest would give guaranteed-empty results.
            "current_teams": sorted(
                {e["current_team"] for e in self.search_index if e["current_team"]}
            ),
        }


def create_app(data_dir=None):
    app = Flask(__name__, static_folder=None)
    # Refuse to emit NaN/Infinity: they are not valid JSON and the browser's JSON.parse
    # rejects them, so a leak should fail loudly here rather than in the page.
    app.json.allow_nan = False
    data = ScoutingData(data_dir)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/summary")
    def summary():
        return jsonify(data.summary())

    @app.get("/api/players")
    def players():
        return jsonify(
            data.search(
                request.args.get("q", ""),
                above_age_only=request.args.get("above_age") == "1",
                birth_year=request.args.get("birth_year", type=int),
                current_team=request.args.get("current_team"),
                limit=request.args.get("limit", default=40, type=int),
            )
        )

    @app.get("/api/player/<player_id>")
    def player(player_id):
        found = data.player(player_id)
        if found is None:
            return jsonify({"error": "unknown player_id"}), 404
        return jsonify(found)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    log.info("scouting dashboard on %s", url)
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
