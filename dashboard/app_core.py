"""Local scouting dashboard over the scraped youth-league data.

Serves a single page plus a small JSON API. Everything is loaded into memory once at
startup and held there, so a request never touches a CSV file or a database.

Two data sources exist behind one shared in-memory model (``BaseScoutingData``):

* ``CsvScoutingData`` reads the CSVs in data/ -- the default, and still what Render
  runs. No network access and no database.
* ``PostgresScoutingData`` (dashboard/postgres_source.py) reads the same shape of
  data from a Postgres database's currently-published dataset instead. It is loaded
  lazily, only when ``DATA_SOURCE=postgres``, so psycopg does not need to be
  installed for the default CSV path to work.

Run the CSV-backed app with:

    python -m dashboard.app
"""

import argparse
import logging
import math
import os
import webbrowser
from collections import defaultdict

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from ifa_scraper import config, name_quality, run

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

# ``אצלנו`` in the movements report is intentionally not configurable: it is
# Hapoel Be'er Sheva.  These are the IDs and exact Hebrew display-name variants
# observed in player_season_stats.csv and player_history.csv at the feature's
# introduction (2026-09-30).  The IFA has published a different team_id for
# youth squads, colours, numbered squads and historical records, so choosing a
# single ID would silently lose club spells.  Conversely, matching every name
# containing "באר שבע" would incorrectly capture Maccabi, M.S. and Otzma Be'er
# Sheva.  Keep this deliberately small, evidence-based allow-list up to date if
# the source publishes a new, verified Hapoel Be'er Sheva form.
HAPOEL_BEER_SHEVA_TEAM_IDS = frozenset(
    {
        "1440", "1443", "1444", "2014", "2085", "2143", "2181", "2294",
        "2755", "2756", "2938", "6248", "6485", "7327", "7447", "7449",
    }
)
HAPOEL_BEER_SHEVA_TEAM_NAMES = frozenset(
    {
        "הפ' באר שבע אדום",
        'הפועל ב"ש',
        'הפועל ב"ש "לבן"',
        'הפועל ב"ש 2',
        'הפועל ב"ש 3',
        'הפועל ב"ש אדום',
        'הפועל ב"ש לבן',
        "הפועל באר שבע",
        "הפועל באר שבע 2",
    }
)


def is_hapoel_beer_sheva_team(row):
    """Return whether a canonical membership belongs to Hapoel Be'er Sheva.

    Team IDs are the strongest evidence, while the exact-name allow-list keeps
    the report resilient when the IFA introduces another ID for an already
    verified published form.  This is purposefully *not* a broad Be'er Sheva
    substring rule; nearby clubs must never become "our club" in this report.
    """
    return (
        _text(row.get("team_id")) in HAPOEL_BEER_SHEVA_TEAM_IDS
        or _text(row.get("team_name")).strip() in HAPOEL_BEER_SHEVA_TEAM_NAMES
    )


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


def _optional_int(value):
    """Return an integer only when the source actually published one."""
    value = _clean(value)
    if value is None or _text(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value, default=False):
    value = _clean(value)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes"}


def _canonical_key(row):
    """Identity for a team aggregate published by the IFA.

    The IFA statistics endpoint is already aggregated per player/team/season, even
    where the same team appears in more than one discovered league listing.  Adding
    a league identifier here would duplicate that aggregate, so the safe key is this
    three-part identity rather than a display league name.
    """
    return (
        _text(row.get("player_id")),
        int(row["season_id"]),
        _text(row.get("team_id")),
    )


def _stat_quality(row):
    """Rank duplicate sources without ever summing the same IFA aggregate twice."""
    completeness = _text(row.get("stats_completeness"))
    return {
        "full": 3,
        "partial": 2,
        "unavailable": 1,
    }.get(completeness, 2 if row.get("stats_available") else 1)


def merge_canonical_rows(rows):
    """Collapse rows sharing a canonical key into one winner per key.

    Used both by ``BaseScoutingData.player()`` (CSV: merging
    ``player_season_stats.csv`` against ``player_history.csv`` at read time)
    and by ``scripts.import_canonical_dataset`` (Postgres: merging the same
    two sources once, at import time, into a single canonical
    ``player_team_seasons`` row per (player_id, team_id, season_id) --
    ``source_file`` is provenance, never part of the row's identity, so at
    most one row may exist per canonical key). Both call sites must produce
    identical results for the same input rows, which is why this lives in one
    place instead of two: a second copy of this algorithm is a parity risk
    the moment one of them drifts.

    Returns ``{canonical_key: merged_row}``. When two rows share a key, the
    later one in ``rows`` wins ties on stats quality (callers pass the row
    they want preferred on a tie last -- CSV passes history rows first, then
    the more detailed recent-export rows second; the importer's row order
    follows the same convention, see ``_load_season_source``).
    """
    merged = {}
    for row in rows:
        key = _canonical_key(row)
        current = merged.get(key)
        if current is None:
            merged[key] = dict(row)
            continue
        if _stat_quality(row) >= _stat_quality(current):
            winner, loser = dict(row), current
        else:
            winner, loser = current, row
        # The winner on numeric-stats quality is not necessarily the winner on
        # name evidence: IFA masks some players' names (a run of asterisks) on
        # some pages while publishing the real name elsewhere for the same
        # player, and a masked name must never displace a known one just
        # because its row happened to carry more complete statistics.
        winner["player_name"] = name_quality.better_name(
            winner.get("player_name"), loser.get("player_name")
        )
        # Retain other descriptive context without combining numeric values:
        # both sources describe the same IFA aggregate. ``league_id`` here is
        # the CSV/scraper's own display text for that row (see the module
        # docstring on why it can be a comma-joined, multi-valued string) --
        # unrelated to any database identity key.
        for field in ("team_name", "league_id", "league_name", "age_group"):
            if not _text(winner.get(field)) and _text(loser.get(field)):
                winner[field] = loser[field]
        merged[key] = winner
    return merged


class BaseScoutingData:
    """In-memory canonical view, indexed for per-player lookups.

    This class holds every method that only cares about *already-loaded* Python
    data structures (search, the player-detail merge, origin inference, career
    totals, the map/venue grouping). It knows nothing about CSVs or Postgres.

    A concrete subclass supplies the five ``_load_*`` methods, each returning
    the same shape regardless of where the data came from:

    * ``_load_players()``       -> {player_id: row} from the player catalog
      (``players_youth.csv`` or the ``players`` table for the current dataset)
    * ``_load_season_rows()``   -> list of recent, detailed player-team-season
      rows (``player_season_stats.csv`` / ``source_file='player_season_stats'``)
    * ``_load_history_rows()``  -> list of broader historical player-team-season
      rows, possibly overlapping the above (``player_history.csv`` /
      ``source_file='player_history'``)
    * ``_load_locations()``     -> {team_id: row} of the team's current venue
    * ``_load_details()``       -> {player_id: {"birth_year": ...}}

    ``season_rows`` and ``history_rows`` are two views of the *same* underlying
    grain -- one row per (player_id, team_id, season_id) -- kept separate
    because they can each publish a row for the same key with different
    completeness (see ``_canonical_key`` and ``player()`` below); the merge
    that picks a winner between them happens once, per player, on read.
    """

    def __init__(self):
        """Run the shared loading sequence.

        A subclass sets whatever source-specific attribute it needs (``data_dir``
        for CSVs, ``database_url`` for Postgres) *before* calling
        ``super().__init__()``, since the ``_load_*`` methods below read it.
        """
        self.players = self._load_players()
        self.season_rows = self._load_season_rows()
        self.history_rows = self._load_history_rows()
        self.locations = self._load_locations()
        self.details = self._load_details()

        self.rows_by_player = defaultdict(list)
        for row in self.season_rows:
            self.rows_by_player[row["player_id"]].append(row)

        self.history_by_player = defaultdict(list)
        for row in self.history_rows:
            self.history_by_player[row["player_id"]].append(row)

        # Career timelines are used by both the player page and the Hapoel
        # Be'er Sheva movements report.  Cache the one canonical interpretation
        # per player so the report remains an in-memory operation rather than
        # rebuilding (or querying) a career for every HTTP request.
        self._timeline_cache = {}
        self._player_movements_cache = None

        # Recomputed here rather than read from the CSV because players_youth.csv only
        # carries the aggregate flag, while the season table shows a flag per spell.
        self.natural_brackets = run.natural_age_groups(self.season_rows, self.details)

        # Search results need the former-Hapoel marker without issuing a second
        # request per row.  This intentionally scans source memberships rather
        # than calling _player_timeline() for the full catalog: canonical
        # timelines are comparatively large and must remain demand-driven.
        (
            self.former_hapoel_player_ids,
            self._hapoel_candidate_player_ids,
        ) = self._derive_hapoel_player_ids()

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
                "former_hapoel_player": p["player_id"] in self.former_hapoel_player_ids,
                "haystack": f"{p['player_name']} {p['player_id']} {_text(p.get('current_team'))}",
            }
            for p in self.players.values()
        ]

    def _load_players(self):
        raise NotImplementedError

    def _load_season_rows(self):
        raise NotImplementedError

    def _load_history_rows(self):
        raise NotImplementedError

    def _load_locations(self):
        raise NotImplementedError

    def _load_details(self):
        raise NotImplementedError

    @staticmethod
    def _normalise_stat_rows(frame, source_name):
        """Read both legacy and canonical CSVs without inventing missing values."""
        frame = frame.copy()
        frame["season_id"] = pd.to_numeric(frame["season_id"], errors="coerce").astype(
            "Int64"
        )
        frame = frame[frame["season_id"].notna()].copy()
        frame["season_id"] = frame["season_id"].astype(int)
        for field in SEASON_STAT_FIELDS:
            if field not in frame:
                frame[field] = None
            frame[field] = pd.to_numeric(frame[field], errors="coerce").astype("Int64")
        for field in ("league_id", "stats_available", "stats_source", "stats_completeness"):
            if field not in frame:
                frame[field] = None

        rows = []
        for row in frame.to_dict("records"):
            published = [_optional_int(row.get(field)) for field in SEASON_STAT_FIELDS]
            inferred_available = any(value is not None for value in published)
            row["stats_available"] = _as_bool(
                row.get("stats_available"), default=inferred_available
            )
            row["stats_source"] = _text(row.get("stats_source")) or (
                "team_player_statistics" if row["stats_available"] else "registration_only"
            )
            row["stats_completeness"] = _text(row.get("stats_completeness")) or (
                "full"
                if row["stats_available"] and all(value is not None for value in published)
                else "partial"
                if row["stats_available"]
                else "unavailable"
            )
            for field, value in zip(SEASON_STAT_FIELDS, published):
                row[field] = value
            rows.append(row)
        return rows

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

    def _player_timeline(self, player_id):
        """Return this player's canonical, chronologically ordered memberships.

        This is the single shared timeline used by ``player()`` and the
        Hapoel Be'er Sheva movement report.  In particular it preserves the
        existing history/recent overlap semantics from ``merge_canonical_rows``:
        a source row is never counted as a second club membership merely
        because it appears in both input files.
        """
        cached = self._timeline_cache.get(player_id)
        if cached is not None:
            return cached
        merged = merge_canonical_rows(
            [*self.history_by_player.get(player_id, []), *self.rows_by_player.get(player_id, [])]
        )
        timeline = sorted(
            merged.values(), key=lambda row: (int(row["season_id"]), _text(row.get("team_name")))
        )
        self._timeline_cache[player_id] = timeline
        return timeline

    def _derive_hapoel_player_ids(self):
        """Return former catalog IDs and all Hapoel-membership candidate IDs.

        The EX badge needs only three facts per player: whether Hapoel was ever
        observed, the latest observed season, and whether *any* membership in
        that season is Hapoel.  It does not need canonical statistics or a
        copied row dictionary.  This is semantically safe across the overlapping
        history and recent sources because membership evidence is idempotent:
        duplicate rows cannot change ``ever_hapoel``, and latest-season Hapoel
        evidence is combined with OR so a same-season external row cannot turn
        a current Hapoel player into a former one.
        """
        state_by_player = {}
        for source_rows in (self.history_rows, self.season_rows):
            for row in source_rows:
                player_id = row["player_id"]
                season_id = int(row["season_id"])
                is_hapoel = is_hapoel_beer_sheva_team(row)
                state = state_by_player.get(player_id)
                if state is None:
                    state_by_player[player_id] = (is_hapoel, season_id, is_hapoel)
                    continue

                ever_hapoel, latest_season_id, latest_season_has_hapoel = state
                if season_id > latest_season_id:
                    state_by_player[player_id] = (
                        ever_hapoel or is_hapoel,
                        season_id,
                        is_hapoel,
                    )
                elif season_id == latest_season_id:
                    state_by_player[player_id] = (
                        ever_hapoel or is_hapoel,
                        latest_season_id,
                        latest_season_has_hapoel or is_hapoel,
                    )
                elif is_hapoel and not ever_hapoel:
                    state_by_player[player_id] = (
                        True,
                        latest_season_id,
                        latest_season_has_hapoel,
                    )

        hapoel_candidate_ids = frozenset(
            player_id
            for player_id, (ever_hapoel, _, _) in state_by_player.items()
            if ever_hapoel
        )
        former_hapoel_player_ids = frozenset(
            player_id
            for player_id, (ever_hapoel, _, latest_season_has_hapoel) in state_by_player.items()
            if (
                player_id in self.players
                and ever_hapoel
                and not latest_season_has_hapoel
            )
        )
        return former_hapoel_player_ids, hapoel_candidate_ids

    @staticmethod
    def _movement_teams(rows):
        """Return a deterministic, de-duplicated public team list for a season."""
        teams = {
            (_text(row.get("team_id")), _text(row.get("team_name")))
            for row in rows
        }
        return [
            {"team_id": team_id, "team_name": team_name}
            for team_id, team_name in sorted(teams, key=lambda team: (team[1], team[0]))
        ]

    @staticmethod
    def _movement_seasons(timeline):
        """Group an already canonical timeline by season in chronological order."""
        grouped = defaultdict(list)
        for row in timeline:
            grouped[int(row["season_id"])].append(row)
        return [(season_id, grouped[season_id]) for season_id in sorted(grouped)]

    def _movement_identity(self, player_id, timeline):
        """Build report identity without constructing the full player-detail view."""
        catalog = self.players.get(player_id, {})
        _, observed_name = name_quality.best_name(
            row.get("player_name") for row in reversed(timeline)
        )
        birth_year = _clean(catalog.get("birth_year"))
        if birth_year is None:
            birth_year = _clean(self.details.get(player_id, {}).get("birth_year"))
        return {
            "player_id": player_id,
            "player_name": observed_name or _text(catalog.get("player_name")) or player_id,
            "birth_year": _optional_int(birth_year),
        }

    def player_movements(self):
        """Report Hapoel Be'er Sheva arrivals and departures from loaded careers.

        IFA rows are season-level registrations, not dated transfer events.  A
        same-season Hapoel/external pair is therefore explicitly labelled
        ``same_season_ambiguous`` instead of inventing an arrival/departure
        direction.  "Previous club" means the nearest earlier *observed*
        season outside the current Hapoel spell; missing seasons do not break a
        spell because historic coverage is incomplete.
        """
        if self._player_movements_cache is not None:
            return self._player_movements_cache

        former_players = []
        current_players = []
        observed_hapoel_ids = set()
        # Only a player with at least one Hapoel membership can appear in this
        # report.  The candidate set is derived from raw source memberships at
        # initialization, so non-Hapoel players never need a canonical career.
        player_ids = sorted(self._hapoel_candidate_player_ids)

        for player_id in player_ids:
            timeline = self._player_timeline(player_id)
            if not timeline:
                continue
            seasons = self._movement_seasons(timeline)
            season_info = []
            for season_id, rows in seasons:
                hapoel_rows = [row for row in rows if is_hapoel_beer_sheva_team(row)]
                external_rows = [row for row in rows if not is_hapoel_beer_sheva_team(row)]
                for row in hapoel_rows:
                    observed_hapoel_ids.add(_text(row.get("team_id")))
                season_info.append(
                    {
                        "season_id": season_id,
                        "season": _text(rows[0].get("season")),
                        "hapoel": hapoel_rows,
                        "external": external_rows,
                    }
                )

            hapoel_seasons = [entry for entry in season_info if entry["hapoel"]]
            if not hapoel_seasons:
                continue
            identity = self._movement_identity(player_id, timeline)
            latest = season_info[-1]

            if latest["hapoel"]:
                # Work backwards across observed seasons, rather than assuming
                # numeric season IDs are contiguous.  A gap in the source is
                # unknown history, not evidence that a spell ended.
                spell = []
                for entry in reversed(season_info):
                    if not entry["hapoel"]:
                        break
                    spell.append(entry)
                spell.reverse()
                spell_start = spell[0]
                ambiguous = any(entry["external"] for entry in spell)
                previous = season_info[len(season_info) - len(spell) - 1] if len(spell) < len(season_info) else None

                # When an external club appears alongside Hapoel in the current
                # spell, season-only data cannot establish which came first.
                # Do not turn that row into a supposed source club.
                if ambiguous:
                    status = "same_season_ambiguous"
                    previous = None
                elif previous is None:
                    status = "no_previous_history"
                else:
                    status = "known"

                current_players.append(
                    {
                        **identity,
                        "current_season": latest["season"],
                        "current_season_id": latest["season_id"],
                        "current_hapoel_teams": self._movement_teams(latest["hapoel"]),
                        "current_spell_start_season": spell_start["season"],
                        "current_spell_start_season_id": spell_start["season_id"],
                        "previous_observed_season": previous["season"] if previous else None,
                        "previous_observed_season_id": previous["season_id"] if previous else None,
                        "previous_clubs": self._movement_teams(previous["external"]) if previous else [],
                        "status": status,
                    }
                )
                continue

            # A former player has Hapoel evidence, is no longer Hapoel in the
            # latest observed season, and has at least one external membership
            # there.  All latest external clubs are retained; one is never
            # picked arbitrarily.
            if not latest["external"]:
                continue
            last_hapoel = hapoel_seasons[-1]
            status = (
                "same_season_ambiguous"
                if last_hapoel["external"]
                else "known"
            )
            former_players.append(
                {
                    **identity,
                    "last_hapoel_season": last_hapoel["season"],
                    "last_hapoel_season_id": last_hapoel["season_id"],
                    "last_hapoel_teams": self._movement_teams(last_hapoel["hapoel"]),
                    "latest_season": latest["season"],
                    "latest_season_id": latest["season_id"],
                    "current_clubs": self._movement_teams(latest["external"]),
                    "status": status,
                }
            )

        former_players.sort(
            key=lambda entry: (-entry["last_hapoel_season_id"], entry["player_name"], entry["player_id"])
        )
        current_players.sort(
            key=lambda entry: (-entry["current_spell_start_season_id"], entry["player_name"], entry["player_id"])
        )
        summary = {
            "former_players": len(former_players),
            "current_players": len(current_players),
            "known_previous_club": sum(entry["status"] == "known" for entry in current_players),
            "no_previous_history": sum(entry["status"] == "no_previous_history" for entry in current_players),
            "ambiguous": sum(
                entry["status"] == "same_season_ambiguous"
                for entry in [*former_players, *current_players]
            ),
        }
        self._player_movements_cache = {
            "club": {
                "name": "הפועל באר שבע",
                "team_ids": sorted(observed_hapoel_ids),
            },
            "former_players": former_players,
            "current_players": current_players,
            "summary": summary,
        }
        return self._player_movements_cache

    def player(self, player_id):
        """Return a player's de-duplicated career, preserving unknown statistics."""
        player = self.players.get(player_id)
        if player is None:
            return None

        rows = self._player_timeline(player_id)

        # The catalog entry's own name (``player["player_name"]``) only reflects
        # whatever built that catalog -- the scraper's own recent-season rows for
        # CsvScoutingData, a plain identity row for PostgresScoutingData -- so a
        # known name that surfaces only in an older season (e.g. player_history.csv,
        # or an earlier dataset) would otherwise never reach the player's own
        # displayed name, even though the exact same row already won the per-season
        # merge above. Every row's name has already been through that merge, so
        # ranking them again (newest first, to keep the same tie-break as
        # elsewhere) picks the best evidence across the player's whole career.
        _, identity_name = name_quality.best_name(
            row.get("player_name") for row in reversed(rows)
        )
        player_name = identity_name or player["player_name"]

        seasons = []
        for row in rows:
            has_stats = bool(row["stats_available"])
            steps = (
                run.age_groups_above(row, self.details, self.natural_brackets)
                if has_stats
                else None
            )
            seasons.append(
                {
                    "season": _text(row.get("season")),
                    "season_id": int(row["season_id"]),
                    "team_id": _text(row.get("team_id")),
                    "team_name": _text(row.get("team_name")),
                    "league_id": _text(row.get("league_id")),
                    "league_name": _text(row.get("league_name")),
                    "age_group": _text(row.get("age_group")),
                    "above_age_steps": steps if steps and steps > 0 else 0,
                    "stats_available": has_stats,
                    "has_stats": has_stats,  # compatibility with existing map consumers
                    "stats_source": row["stats_source"],
                    "stats_completeness": row["stats_completeness"],
                    "registered_no_games": has_stats and row.get("games") == 0,
                    **{field: row.get(field) for field in SEASON_STAT_FIELDS},
                }
            )

        teams, venues = self._career_track(seasons)
        totals = self._career_totals(seasons)
        seasons_played = []
        for entry in sorted(seasons, key=lambda e: e["season_id"], reverse=True):
            if entry["season"] and entry["season"] not in seasons_played:
                seasons_played.append(entry["season"])

        first_club = teams[0] if teams else {}
        origin = self._infer_likely_origin(teams, seasons, _clean(player.get("birth_year")))
        return {
            "player_id": player_id,
            "player_name": player_name,
            "birth_year": _clean(player.get("birth_year")),
            "image_url": _text(player.get("image_url")) or None,
            "current_team": _text(player.get("current_team")),
            "num_teams": len(teams),
            "age_groups": _text(player.get("age_groups")),
            "leagues": _text(player.get("leagues")),
            "seasons_played": ", ".join(seasons_played),
            "plays_above_age": player.get("plays_above_age") == "Yes",
            "age_groups_above": int(player.get("age_groups_above") or 0),
            "above_age_history": _text(player.get("above_age_history")),
            "former_hapoel_player": player_id in self.former_hapoel_player_ids,
            "totals": totals,
            "seasons": seasons[::-1],
            "teams": teams,
            "venues": venues,
            # Exposed now so the next enhancement (origin/hometown inference) does not
            # need another data-model change.  The city may be empty until venues are
            # rerun after building player_history.csv.
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
    def _career_totals(seasons):
        """Sum only published values across the complete deduplicated timeline."""
        totals = {field: 0 for field in SEASON_STAT_FIELDS}
        for season in seasons:
            if not season["stats_available"]:
                continue
            for field in SEASON_STAT_FIELDS:
                value = season.get(field)
                if value is not None:
                    totals[field] += value
        games = totals["games"]
        return {
            "goals_total": totals["goals"],
            # Historical team aggregates cannot be split reliably by competition.
            "goals_league": None,
            "goals_cup": None,
            "games_total": games,
            "minutes_total": totals["minutes"],
            "avg_minutes_per_game": round(totals["minutes"] / games, 1) if games else None,
            "starts": totals["starts"],
            "sub_on": totals["sub_on"],
            "sub_off": totals["sub_off"],
            "yellow_cards_total": totals["yellow_cards_league_cup"] + totals["yellow_cards_toto"],
            "red_cards": totals["red_cards"],
        }

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

        # If team_locations.csv has not yet been regenerated for historical clubs, the
        # city is genuinely unknown.  Do not turn the club name itself into a hometown
        # assertion here; venues.py is the authoritative club-to-locality layer.
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
            # History rows produced by the bulk scraper normally always have a team id.
            # Falling back to the name keeps the dashboard robust to hand-edited files.
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
                        for item in (self.history_rows or self.season_rows)
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


class CsvScoutingData(BaseScoutingData):
    """Loads the canonical view from the CSVs in data/ -- the default data source."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or config.DATA_DIR
        super().__init__()

    def _load_players(self):
        path = self.data_dir / "players_youth.csv"
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"player_id": str})
        frame["birth_year"] = pd.to_numeric(frame["birth_year"], errors="coerce").astype(
            "Int64"
        )
        if "name_status" not in frame:
            # Older, already-generated files predate the name_status column; derive
            # it from the name itself rather than treating every row as unknown.
            frame["name_status"] = frame["player_name"].map(name_quality.classify_name)
        log.info("loaded %s players", len(frame))
        return {row["player_id"]: row for row in frame.to_dict("records")}

    def _load_season_rows(self):
        path = self.data_dir / "player_season_stats.csv"
        frame = pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype={"player_id": str, "team_id": str, "league_id": str},
        )
        log.info("loaded %s player-season rows", len(frame))
        return self._normalise_stat_rows(frame, source_name=path.name)

    def _load_history_rows(self):
        """Load historical rows, retaining their source-level availability metadata."""
        path = self.data_dir / "player_history.csv"
        if not path.exists():
            log.warning("%s missing; player pages will show detailed seasons only", path.name)
            return []
        frame = pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype={"player_id": str, "team_id": str, "league_id": str},
        )
        # The full historical export intentionally records players who are no longer
        # in the recent scouting dataset.  They cannot be opened through this
        # dashboard, so retaining their hundreds of thousands of dictionaries in the
        # web process serves nobody and can prevent a small deployment from booting.
        frame = frame[frame["player_id"].isin(self.players)].copy()
        log.info("loaded %s historical player-team-season rows", len(frame))
        return self._normalise_stat_rows(frame, source_name=path.name)

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


# Backward-compatible alias: existing code and tests that import ``ScoutingData``
# directly (it defaults to the CSV source) keep working unchanged.
ScoutingData = CsvScoutingData


def _resolve_data_source(data_dir, data_source):
    """Pick the concrete BaseScoutingData subclass named by DATA_SOURCE.

    ``postgres_source`` is imported lazily, inside this function, so that the
    default CSV path works even when psycopg/SQLAlchemy are not installed --
    the same convention dashboard/auth.py already uses for PostgresUserStore.
    """
    source = (data_source or os.environ.get("DATA_SOURCE") or "csv").strip().lower()
    if source == "csv":
        return CsvScoutingData(data_dir)
    if source == "postgres":
        from .postgres_source import PostgresScoutingData

        return PostgresScoutingData(database_url=os.environ.get("DATABASE_URL"))
    raise RuntimeError(f"unknown DATA_SOURCE {source!r}; expected 'csv' or 'postgres'")


def create_app(data_dir=None, data_source=None):
    app = Flask(__name__, static_folder=None)
    # Refuse to emit NaN/Infinity: they are not valid JSON and the browser's JSON.parse
    # rejects them, so a leak should fail loudly here rather than in the page.
    app.json.allow_nan = False
    data = _resolve_data_source(data_dir, data_source)
    # Extension rather than a module global: authenticated application-state
    # routes can validate a player against this same loaded catalog, while test
    # factories and simultaneous app instances remain isolated.
    app.extensions["scouting_data"] = data

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

    @app.get("/api/player-movements")
    def player_movements():
        """Return the fixed Hapoel Be'er Sheva movement report from memory."""
        return jsonify(data.player_movements())

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
