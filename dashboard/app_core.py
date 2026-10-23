"""Local scouting dashboard over the club's youth-league data.

Serves a single page plus a small JSON API. Everything is read from the normalised
schema export in data/db/ (see dashboard/db.py) once at startup and held in memory, so
the dashboard needs no network access and no database server. Run it with:

    python -m dashboard.app
"""

import argparse
import bisect
import csv
import logging
import math
import os
import statistics
import tempfile
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from ifa_scraper import config

from .db import SEASON_STAT_FIELDS, SchemaData

log = logging.getLogger(__name__)

STATIC_DIR = config.PROJECT_ROOT / "dashboard" / "static"
# The scraper keeps writing its own CSVs to data/; the dashboard reads only the schema.
DB_DIR = config.DATA_DIR / "db"

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

# Regulation match length per age bracket. The younger brackets play shorter halves,
# so per-match rates are normalised per bracket rather than per 90 minutes.
MATCH_MINUTES = {"נוער": 90, "נערים א": 90, "נערים ב": 80, "נערים ג": 80}
DEFAULT_MATCH_MINUTES = 90
# Below this many minutes a scoring rate is too noisy to rank against peers.
MIN_MINUTES_FOR_RANK = 450
# A season becomes the reference season for the tiles once a typical team has played
# this many games, so a season that has only just started does not hide the last one.
MIN_TEAM_GAMES_FOR_SEASON = 10


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


def _int_or_none(value):
    """Read a numeric cell as an int, keeping a missing value as None (not zero)."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return int(float(cleaned))
    except (TypeError, ValueError):
        return None


def _flag(value):
    """Read a yes/no cell written as 1/0, true/false or yes/no; None when unknown."""
    text = _text(value).strip().lower()
    if text in {"1", "1.0", "true", "yes", "y"}:
        return True
    if text in {"0", "0.0", "false", "no", "n"}:
        return False
    return None


FAVORITES_FIELDS = ["player_id", "created_at", "notes"]


class FavoritesStore:
    """The scout's favourite players, persisted to ``data/db/Favorite_Players.csv``.

    The file holds only what the scout adds (id, when, notes). Names and clubs are
    joined from the Players and Teams tables when read, so they stay current.
    Every change rewrites the whole file, which is tiny, and the in-memory copy is only
    updated once the write has succeeded.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write({})
            log.info("created %s", self.path.name)
        self._rows = self._read()
        log.info("loaded %s favourites", len(self._rows))

    def _read(self):
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            return {
                row["player_id"].strip(): {
                    "created_at": row.get("created_at") or "",
                    "notes": row.get("notes") or "",
                }
                for row in csv.DictReader(handle)
                if (row.get("player_id") or "").strip()
            }

    def _write(self, rows):
        # Write a sibling temp file and swap it in, so a crash mid-write can never
        # leave a truncated favourites file behind. BOM and LF line ends match the
        # SQL Server export the file came from (and the BOM lets Excel read Hebrew).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".favorites-", suffix=".csv"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=FAVORITES_FIELDS, lineterminator="\n")
                writer.writeheader()
                for player_id, row in rows.items():
                    writer.writerow({"player_id": player_id, **row})
            os.replace(temp_path, self.path)
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def all(self):
        with self._lock:
            return {player_id: dict(row) for player_id, row in self._rows.items()}

    def get(self, player_id):
        with self._lock:
            row = self._rows.get(player_id)
            return dict(row) if row else None

    def upsert(self, player_id, notes=None):
        """Add a favourite, or update its notes; ``notes=None`` keeps existing notes."""
        with self._lock:
            rows = {key: dict(row) for key, row in self._rows.items()}
            row = rows.setdefault(
                player_id,
                {
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "notes": "",
                },
            )
            if notes is not None:
                row["notes"] = notes
            self._write(rows)
            self._rows = rows
            return dict(row)

    def remove(self, player_id):
        with self._lock:
            if player_id not in self._rows:
                return False
            rows = {key: dict(row) for key, row in self._rows.items() if key != player_id}
            self._write(rows)
            self._rows = rows
            return True


class ScoutingData:
    """In-memory view of the data/db/ schema, indexed for per-player lookups."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or DB_DIR
        schema = SchemaData(self.data_dir)
        self.players = schema.players
        # Rows with statistics, and history rows whose statistics are all empty.
        self.season_rows = schema.season_rows
        self.history_rows = schema.history_rows
        self.locations = schema.locations
        self.details = schema.details
        self.team_names = schema.team_names
        self.competition_names = schema.competition_names
        self.favorites = FavoritesStore(self.data_dir / "Favorite_Players.csv")
        # None (rather than empty) means the weekly match import has not run yet,
        # which the UI reports differently from a player with no matches in it.
        self.matches_by_player = self._index_match_rows(schema.match_rows)

        self.rows_by_player = defaultdict(list)
        for row in self.season_rows:
            self.rows_by_player[row["player_id"]].append(row)

        self.history_by_player = defaultdict(list)
        for row in self.history_rows:
            self.history_by_player[row["player_id"]].append(row)

        self._build_season_metrics()

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
            # A fixed base order (top scorers first, then player id), so that results
            # tied on the search's own sort key always come back in the same order.
            for p in sorted(
                self.players.values(),
                key=lambda p: (-p["goals_total"], -p["minutes_total"], p["player_id"]),
            )
        ]

    @staticmethod
    def _index_match_rows(frame):
        """Index Player_Match_Stats rows newest first per player; None when empty."""
        if frame is None:
            log.info("Player_Match_Stats is empty; recent matches will show a placeholder")
            return None
        frame = frame.copy()
        # The IFA site writes dates day-first (dd/mm/yyyy); ISO dates parse either way.
        frame["match_dt"] = pd.to_datetime(
            frame["match_date"], errors="coerce", dayfirst=True, format="mixed"
        )
        frame = frame.sort_values(["match_dt", "match_id"], ascending=False, na_position="last")
        matches = defaultdict(list)
        for row in frame.to_dict("records"):
            matches[_text(row.get("player_id"))].append(row)
        log.info("loaded %s player-match rows", len(frame))
        return matches

    def recent_matches(self, player_id, limit=5):
        if self.matches_by_player is None:
            return {"available": False, "matches": []}

        matches = []
        for row in self.matches_by_player.get(player_id, [])[:limit]:
            team_id = _text(row.get("team_id"))
            opponent_id = _text(row.get("opponent_team_id"))
            competition_id = _text(row.get("competition_id"))
            played_on = _clean(row.get("match_dt"))
            matches.append(
                {
                    "match_id": _text(row.get("match_id")),
                    "match_date": played_on.strftime("%Y-%m-%d")
                    if played_on is not None
                    else _text(row.get("match_date")),
                    "season_id": _int_or_none(row.get("season_id")),
                    "team_id": team_id,
                    "team_name": self.team_names.get(team_id, ""),
                    "opponent_team_id": opponent_id,
                    "opponent_name": self.team_names.get(opponent_id, ""),
                    "competition_id": competition_id,
                    "competition_name": self.competition_names.get(competition_id, ""),
                    "minutes_played": _int_or_none(row.get("minutes_played")),
                    "is_starter": _flag(row.get("is_starter")),
                    "goals": _int_or_none(row.get("goals")),
                    "yellow_cards": _int_or_none(row.get("yellow_cards")),
                    "red_cards": _int_or_none(row.get("red_cards")),
                }
            )
        return {"available": True, "matches": matches}

    def favorite_entry(self, player_id, row):
        """A favourite joined with the player's current identity from the scraped data."""
        player = self.players.get(player_id) or {}
        return {
            "player_id": player_id,
            "created_at": row["created_at"],
            "notes": row["notes"],
            "player_name": _text(player.get("player_name")),
            "current_team": _text(player.get("current_team")),
            "birth_year": _clean(player.get("birth_year")),
        }

    def favorites_list(self):
        entries = [
            self.favorite_entry(player_id, row)
            for player_id, row in self.favorites.all().items()
        ]
        # Newest first: the player just starred is the one the scout wants to see.
        entries.sort(key=lambda entry: entry["created_at"], reverse=True)
        return entries

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

    def _build_season_metrics(self):
        """Pre-aggregate Player_Season_Stats for the per-season tile metrics.

        The schema has no team fixture list, so a team's game count is taken as the
        most games any of its players logged that season. History rows (no statistics)
        never reach this, since they are not in ``season_rows``.
        """
        team_games = defaultdict(int)
        team_goals = defaultdict(int)
        for row in self.season_rows:
            key = (int(row["season_id"]), _text(row.get("team_id")))
            team_games[key] = max(team_games[key], _int_or_none(row.get("games")) or 0)
            team_goals[key] += _int_or_none(row.get("goals")) or 0

        games_by_season = defaultdict(list)
        for (season_id, _), games in team_games.items():
            games_by_season[season_id].append(games)
        mature = [
            season_id
            for season_id, games in games_by_season.items()
            if statistics.median(games) >= MIN_TEAM_GAMES_FOR_SEASON
        ]
        self.reference_season_id = max(mature or games_by_season, default=None)

        # (player_id, season_id) -> totals across every team the player had that season.
        aggregates = {}
        for row in self.season_rows:
            games = _int_or_none(row.get("games")) or 0
            if _int_or_none(row.get("minutes")) is None:
                # Without minutes the per-match rates would read the row as 0 minutes
                # played; such a partially filled row is left out of the metrics.
                games = 0
            season_id = int(row["season_id"])
            key = (row["player_id"], season_id)
            agg = aggregates.setdefault(
                key,
                {
                    "season_id": season_id,
                    "season": _text(row.get("season")),
                    "games": 0,
                    "goals": 0,
                    "minutes": 0,
                    "starts": 0,
                    "sub_off": 0,
                    "full_matches": 0.0,
                    "team_goals": 0,
                    "possible_minutes": 0,
                    "minutes_by_age_group": defaultdict(int),
                },
            )
            if not games:
                # A squad listing without appearances says nothing about the player's
                # share of that team, so it must not dilute the team-share metrics.
                continue
            age_group = _text(row.get("age_group"))
            match_minutes = MATCH_MINUTES.get(age_group, DEFAULT_MATCH_MINUTES)
            minutes = _int_or_none(row.get("minutes")) or 0
            team_key = (season_id, _text(row.get("team_id")))
            agg["games"] += games
            agg["goals"] += _int_or_none(row.get("goals")) or 0
            agg["minutes"] += minutes
            agg["starts"] += _int_or_none(row.get("starts")) or 0
            agg["sub_off"] += _int_or_none(row.get("sub_off")) or 0
            agg["full_matches"] += minutes / match_minutes
            agg["team_goals"] += team_goals[team_key]
            agg["possible_minutes"] += team_games[team_key] * match_minutes
            agg["minutes_by_age_group"][age_group] += minutes

        self.season_aggregates = {}
        self.played_seasons = defaultdict(list)
        rate_pools = defaultdict(list)
        for key, agg in aggregates.items():
            by_age = agg.pop("minutes_by_age_group")
            # Peers are the bracket where the player spent most of that season.
            agg["age_group"] = max(by_age, key=by_age.get) if by_age else ""
            self.season_aggregates[key] = agg
            if agg["games"]:
                self.played_seasons[key[0]].append(key[1])
            if agg["minutes"] >= MIN_MINUTES_FOR_RANK and agg["full_matches"]:
                rate_pools[(agg["season_id"], agg["age_group"])].append(
                    agg["goals"] / agg["full_matches"]
                )
        self.rate_pools = {key: sorted(values) for key, values in rate_pools.items()}

    @staticmethod
    def _rates(agg):
        return {
            "goals_per_match": agg["goals"] / agg["full_matches"]
            if agg["full_matches"]
            else None,
            # Capped at 1: the team game count is an estimate and can run short.
            "minutes_share": min(agg["minutes"] / agg["possible_minutes"], 1.0)
            if agg["possible_minutes"]
            else None,
        }

    def season_metrics(self, player_id):
        """Tile metrics for the reference season, or the player's latest played one."""
        played = sorted(self.played_seasons.get(player_id, []))
        if not played:
            return None
        current_id = self.reference_season_id if self.reference_season_id in played else played[-1]
        previous_id = max((sid for sid in played if sid < current_id), default=None)

        agg = self.season_aggregates[(player_id, current_id)]
        rates = self._rates(agg)

        percentile = None
        pool = self.rate_pools.get((current_id, agg["age_group"]), [])
        if len(pool) > 1 and agg["minutes"] >= MIN_MINUTES_FOR_RANK:
            # Share of the *other* peers this player scores more often than; the player
            # is in the pool, and ties are not "beaten". Floored, so only the very top
            # scorer reaches 100.
            beaten = bisect.bisect_left(pool, rates["goals_per_match"])
            percentile = math.floor(100 * beaten / (len(pool) - 1))

        previous = None
        if previous_id is not None:
            previous_agg = self.season_aggregates[(player_id, previous_id)]
            previous = {"season": previous_agg["season"], **self._rates(previous_agg)}

        def ratio(part, whole):
            return part / whole if whole else None

        return {
            "season": agg["season"],
            "season_id": current_id,
            "is_reference_season": current_id == self.reference_season_id,
            "age_group": agg["age_group"],
            "games": agg["games"],
            "goals": agg["goals"],
            "minutes": agg["minutes"],
            "starts": agg["starts"],
            "goals_per_match": rates["goals_per_match"],
            "goals_percentile": percentile,
            "rank_pool_size": len(pool),
            "min_minutes_for_rank": MIN_MINUTES_FOR_RANK,
            "team_goals": agg["team_goals"],
            "team_goal_share": ratio(agg["goals"], agg["team_goals"]),
            "minutes_share": rates["minutes_share"],
            "start_rate": ratio(agg["starts"], agg["games"]),
            # A start without being substituted off means the player saw the match out.
            "full_matches_played": max(agg["starts"] - agg["sub_off"], 0),
            "previous": previous,
        }

    def player(self, player_id):
        """Full detail for one player, including club history before the stats window.

        Player_Season_Stats rows with statistics and history rows (same table, all
        statistics empty) are unioned here; history rows deliberately expose ``None``
        statistics so the UI renders an em dash instead of claiming zero appearances.
        """
        player = self.players.get(player_id)
        if player is None:
            return None

        stats_rows = self.rows_by_player.get(player_id, [])
        stats_by_key = {
            (int(row["season_id"]), _text(row.get("team_id"))): row
            for row in stats_rows
        }

        # Start with historical identity rows when available, then add any detailed
        # rows missing from an older/stale history file.
        merged = {}
        for row in self.history_by_player.get(player_id, []):
            key = (int(row["season_id"]), _text(row.get("team_id")))
            merged[key] = row
        for row in stats_rows:
            key = (int(row["season_id"]), _text(row.get("team_id")))
            merged.setdefault(key, row)

        rows = sorted(
            merged.values(),
            key=lambda r: (int(r["season_id"]), _text(r.get("team_name"))),
        )

        seasons = []
        for row in rows:
            key = (int(row["season_id"]), _text(row.get("team_id")))
            detailed = stats_by_key.get(key)
            source = detailed or row
            has_stats = detailed is not None
            # Computed for history rows too: it depends on the bracket, not the stats.
            steps = source.get("above_age_steps")
            seasons.append(
                {
                    "season": _text(source.get("season")),
                    "season_id": int(source["season_id"]),
                    "team_id": _text(source.get("team_id")),
                    "team_name": _text(source.get("team_name")),
                    "age_group": _text(source.get("age_group")),
                    "league_name": _text(source.get("league_name")),
                    "above_age_steps": steps if steps and steps > 0 else 0,
                    "has_stats": has_stats,
                    # A column missing from an otherwise detailed row stays None too.
                    **{
                        field: _int_or_none(detailed.get(field)) if has_stats else None
                        for field in SEASON_STAT_FIELDS
                    },
                }
            )

        teams, venues = self._career_track(seasons)
        seasons_played = []
        for entry in sorted(seasons, key=lambda e: e["season_id"], reverse=True):
            if entry["season"] and entry["season"] not in seasons_played:
                seasons_played.append(entry["season"])

        first_club = teams[0] if teams else {}
        origin = self._infer_likely_origin(teams, seasons, _clean(player.get("birth_year")))
        current_spell = self._current_spell(rows, _text(player.get("current_team")))
        return {
            "player_id": player_id,
            "player_name": player["player_name"],
            "birth_year": _clean(player.get("birth_year")),
            "image_url": _text(player.get("image_url")) or None,
            "current_team": _text(player.get("current_team")),
            "current_age_group": _text(current_spell.get("age_group")),
            "num_teams": len(teams),
            "age_groups": _text(player.get("age_groups")),
            "leagues": _text(player.get("leagues")),
            "seasons_played": ", ".join(seasons_played),
            "plays_above_age": player.get("plays_above_age") == "Yes",
            "age_groups_above": int(player.get("age_groups_above") or 0),
            "above_age_history": _text(player.get("above_age_history")),
            "totals": {f: _clean(player.get(f)) for f in TOTAL_FIELDS},
            "metrics": self.season_metrics(player_id),
            "seasons": seasons[::-1],
            "teams": teams,
            "venues": venues,
            # The city comes from the first club's venue; it is empty when that team
            # has no venue in the schema.
            "first_club": first_club.get("team_name") or None,
            "first_club_city": first_club.get("city") or None,
            "first_registered_season": origin["first_registered_season"],
            "likely_origin_city": origin["city"],
            "likely_origin_confidence": origin["confidence"],
            "likely_origin_score": origin["score"],
            "likely_origin_candidates": origin["candidates"],
            "likely_origin_basis": origin["basis"],
        }

    @staticmethod
    def _current_spell(stats_rows, current_team):
        """The player's latest-season row from player_season_stats.csv.

        A player can be registered with more than one squad in a season (e.g. playing
        up a bracket at the same club), so the row matching the current team wins, and
        otherwise the one with the most minutes.
        """
        if not stats_rows:
            return {}
        latest = max(int(row["season_id"]) for row in stats_rows)
        rows = [row for row in stats_rows if int(row["season_id"]) == latest]
        return max(
            rows,
            key=lambda row: (
                _text(row.get("team_name")) == current_team,
                _int_or_none(row.get("minutes")) or 0,
            ),
        )

    @staticmethod
    def _season_start_year(season_label):
        """Return the calendar year at the start of an IFA season label.

        The site normally uses labels such as ``2022/23``.  Returning ``None`` rather
        than guessing keeps the origin-confidence calculation conservative when a
        historical row has been edited or imported with another format.
        """
        text = _text(season_label).strip()
        if len(text) < 4 or not text[:4].isdigit():
            return None
        return int(text[:4])

    def _infer_likely_origin(self, teams, seasons, birth_year):
        """Infer a player's likely origin area from their earliest registered club.

        This is deliberately an *origin-area estimate*, not a claimed home address.
        Children often travel to a neighbouring city to play, so even a perfectly
        located first club never receives ``high`` confidence.  Confidence reflects
        three pieces of evidence:

        * how early in the player's youth career the first observed registration is;
        * whether all clubs in that earliest season point to the same locality; and
        * how reliably the club locality itself was resolved by ``venues.py``.

        If the earliest observed season contains clubs in different cities, no single
        city is returned.  The alternatives are exposed through ``candidates`` so the
        UI can be honest about the ambiguity instead of choosing arbitrarily.
        """
        if not teams or not seasons:
            return {
                "city": None,
                "confidence": None,
                "score": None,
                "candidates": [],
                "first_registered_season": None,
                "basis": None,
            }

        first_season_id = min(int(entry["season_id"]) for entry in seasons)
        earliest_seasons = [
            entry for entry in seasons if int(entry["season_id"]) == first_season_id
        ]
        first_registered_season = next(
            (_text(entry.get("season")) for entry in earliest_seasons if entry.get("season")),
            None,
        )

        earliest_teams = [
            team for team in teams if int(team["first_season_id"]) == first_season_id
        ]
        candidate_cities = []
        for team in earliest_teams:
            city = _text(team.get("city")).strip()
            if city and city not in candidate_cities:
                candidate_cities.append(city)

        # If the earliest clubs have no venue city in the schema, the city is genuinely
        # unknown.  Do not turn the club name itself into a hometown assertion here;
        # the Venues table is the authoritative club-to-locality layer.
        if not candidate_cities:
            return {
                "city": None,
                "confidence": None,
                "score": None,
                "candidates": [],
                "first_registered_season": first_registered_season,
                "basis": "first_registered_club",
            }

        if len(candidate_cities) > 1:
            return {
                "city": None,
                "confidence": "ambiguous",
                "score": None,
                "candidates": candidate_cities,
                "first_registered_season": first_registered_season,
                "basis": "multiple_first_season_clubs",
            }

        city = candidate_cities[0]
        season_start = self._season_start_year(first_registered_season)
        approx_age = None
        try:
            year = int(birth_year) if birth_year is not None else None
        except (TypeError, ValueError):
            year = None
        if season_start is not None and year is not None:
            approx_age = season_start - year

        # The score is intentionally capped below a level we would call "high": club
        # location is evidence about a player's likely catchment area, not residence.
        if approx_age is not None and approx_age <= 12:
            score = 0.70
        elif approx_age is not None and approx_age <= 14:
            score = 0.58
        else:
            score = 0.48

        precisions = {
            _text(team.get("precision"))
            for team in earliest_teams
            if _text(team.get("city")).strip() == city
        }
        if precisions and precisions <= {"fallback", "unresolved", ""}:
            score -= 0.12
        elif "fallback" in precisions:
            score -= 0.06

        score = round(max(0.0, min(score, 0.75)), 2)
        confidence = "medium" if score >= 0.60 else "low"
        club_names = []
        for team in earliest_teams:
            name = _text(team.get("team_name")).strip()
            if name and name not in club_names:
                club_names.append(name)

        return {
            "city": city,
            "confidence": confidence,
            "score": score,
            "candidates": candidate_cities,
            "first_registered_season": first_registered_season,
            "basis": {
                "method": "first_registered_club",
                "clubs": club_names,
                "season": first_registered_season,
                "approx_age": approx_age,
            },
        }

    def _career_track(self, seasons):
        """Build club spells and map markers from the complete career timeline.

        Statistics may cover only the recent seasons.  The spell keeps track of that
        distinction so historical clubs can be mapped without pretending that missing
        old statistics are zeros.
        """
        spells = {}
        for entry in seasons:
            team_id = _text(entry.get("team_id"))
            # Schema rows always carry a team id (it is a foreign key). Falling back to
            # the name keeps the dashboard robust to hand-edited files.
            key = team_id or f"name:{entry['team_name']}"
            games = int(entry.get("games") or 0)
            goals = int(entry.get("goals") or 0)
            minutes = int(entry.get("minutes") or 0)
            has_stats = bool(entry.get("has_stats"))
            spell = spells.get(key)
            if spell is None:
                spells[key] = {
                    "team_id": team_id,
                    "team_name": entry["team_name"],
                    "first_season_id": entry["season_id"],
                    "last_season_id": entry["season_id"],
                    "seasons": [entry["season"]],
                    "age_groups": [entry["age_group"]] if entry["age_group"] else [],
                    "games": games,
                    "goals": goals,
                    "minutes": minutes,
                    "above_age_steps": entry["above_age_steps"],
                    "has_detailed_stats": has_stats,
                    "stats_complete": has_stats,
                }
                continue
            spell["first_season_id"] = min(spell["first_season_id"], entry["season_id"])
            spell["last_season_id"] = max(spell["last_season_id"], entry["season_id"])
            if entry["season"] not in spell["seasons"]:
                spell["seasons"].append(entry["season"])
            if entry["age_group"] and entry["age_group"] not in spell["age_groups"]:
                spell["age_groups"].append(entry["age_group"])
            spell["games"] += games
            spell["goals"] += goals
            spell["minutes"] += minutes
            spell["above_age_steps"] = max(
                spell["above_age_steps"], entry["above_age_steps"]
            )
            spell["has_detailed_stats"] = spell["has_detailed_stats"] or has_stats
            spell["stats_complete"] = spell["stats_complete"] and has_stats

        track = sorted(spells.values(), key=lambda item: item["first_season_id"])
        latest = max((spell["last_season_id"] for spell in track), default=None)

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
                    "has_detailed_stats": spell["has_detailed_stats"],
                    "stats_complete": spell["stats_complete"],
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
            venue["has_detailed_stats"] = (
                venue["has_detailed_stats"] or spell["has_detailed_stats"]
            )
            venue["stats_complete"] = venue["stats_complete"] and spell["stats_complete"]

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
            "history_seasons": [
                season
                for _, season in sorted(
                    {
                        (int(item["season_id"]), _text(item.get("season")))
                        # Every season any row covers, with or without statistics.
                        for item in self.season_rows + self.history_rows
                        if _text(item.get("season"))
                    },
                    reverse=True,
                )
            ],
            "history_rows": len(self.history_rows),
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

    @app.get("/api/player/<player_id>/matches")
    def player_matches(player_id):
        if player_id not in data.players:
            return jsonify({"error": "unknown player_id"}), 404
        return jsonify(data.recent_matches(player_id))

    @app.get("/api/favorites")
    def favorites():
        return jsonify(data.favorites_list())

    @app.put("/api/favorites/<player_id>")
    def save_favorite(player_id):
        if player_id not in data.players:
            return jsonify({"error": "unknown player_id"}), 404
        body = request.get_json(silent=True) or {}
        notes = body.get("notes")
        if notes is not None and not isinstance(notes, str):
            return jsonify({"error": "notes must be text"}), 400
        try:
            row = data.favorites.upsert(player_id, notes)
        except PermissionError:
            # Most often favorites.csv is open in Excel, which locks it on Windows.
            return jsonify({"error": "favorites.csv is locked by another program"}), 503
        return jsonify(data.favorite_entry(player_id, row))

    @app.delete("/api/favorites/<player_id>")
    def remove_favorite(player_id):
        try:
            removed = data.favorites.remove(player_id)
        except PermissionError:
            return jsonify({"error": "favorites.csv is locked by another program"}), 503
        if not removed:
            return jsonify({"error": "not a favourite"}), 404
        return jsonify({"player_id": player_id, "removed": True})

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
