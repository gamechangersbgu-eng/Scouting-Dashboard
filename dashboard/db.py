"""Load the normalised schema in data/db/ into the dashboard's in-memory shape.

data/db/ is an export of the club's SQL Server database: one CSV per table (Players,
Teams, Venues, Seasons, Competitions, Age_Groups, Player_Season_Stats,
Player_Match_Stats, Favorite_Players). This module joins those tables back into the
per-player and per-row records the rest of the dashboard works with, and derives what
the scraper used to precompute into players_youth.csv (current team, totals, playing
above age) from Player_Season_Stats, using the same rules as ifa_scraper/run.py.

Two properties matter for future history imports, which add old seasons and kids
leagues to the same tables rather than a separate history table:

* Any number of seasons works; nothing assumes the three seasons of today.
* A Player_Season_Stats row whose statistics are all empty is a history row: it still
  shows where the player was registered, but never counts as zero appearances. Rows with
  only some statistics missing keep the missing ones as None, never as zero.
"""

import logging
from collections import defaultdict

import pandas as pd

from ifa_scraper import config

log = logging.getLogger(__name__)

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

# "Playing above age" is only defined on the youth ladder (נערים ג' up to נוער). Kids
# leagues may appear in a player's history, but moving from ילדים into נערים is normal
# ageing, not playing up, so those rows never get a step count.
YOUTH_AGE_GROUPS = frozenset(config.AGE_GROUP_LADDER)


def read_table(db_dir, name, required=True):
    """Read one exported table as text; SQL Server's NULL becomes an empty cell."""
    path = db_dir / f"{name}.csv"
    if not path.exists():
        if required:
            raise FileNotFoundError(f"{path} is missing; data/db/ needs all schema tables")
        return None
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    return frame.replace({"NULL": ""})


def _int(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _float(value):
    value = (value or "").strip()
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _round_half_up(value, places=1):
    """Round like a person would (2.25 -> 2.3), matching the scraper's averages."""
    factor = 10**places
    return int(value * factor + 0.5) / factor


class SchemaData:
    """Every table of data/db/, joined into the records the dashboard indexes."""

    def __init__(self, db_dir):
        self.db_dir = db_dir
        seasons = read_table(db_dir, "Seasons")
        age_groups = read_table(db_dir, "Age_Groups")
        competitions = read_table(db_dir, "Competitions")
        venues = read_table(db_dir, "Venues")
        teams = read_table(db_dir, "Teams")
        players = read_table(db_dir, "Players")
        stats = read_table(db_dir, "Player_Season_Stats")

        self.season_names = dict(zip(seasons.season_id, seasons.season_name))
        age_group_names = dict(zip(age_groups.age_group_id, age_groups.age_group_name))
        # Youngest to oldest by the table's own max_age, restricted to the youth ladder.
        ordered = age_groups.assign(order=age_groups.max_age.map(_int)).sort_values("order")
        self.age_ladder = [name for name in ordered.age_group_name if name in YOUTH_AGE_GROUPS]

        self.competitions = {
            row.competition_id: {
                "name": row.competition_name,
                "age_group": age_group_names.get(row.age_group_id, ""),
            }
            for row in competitions.itertuples()
        }
        self.competition_names = {cid: c["name"] for cid, c in self.competitions.items()}
        self.team_names = dict(zip(teams.team_id, teams.team_name))

        venue_by_id = {row.venue_id: row for row in venues.itertuples()}
        self.locations = {}
        for team in teams.itertuples():
            venue = venue_by_id.get(team.venue_id)
            self.locations[team.team_id] = {
                "team_id": team.team_id,
                "team_name": team.team_name,
                "field_name": venue.venue_name if venue else "",
                "address": venue.field_address if venue else "",
                "city": venue.field_city if venue else "",
                "lat": _float(venue.latitude) if venue else None,
                "lon": _float(venue.longitude) if venue else None,
                "precision": getattr(venue, "geocode_precision", "") if venue else "",
            }

        self.details = {
            row.player_id: {"birth_year": _int(row.birth_year), "birth_month": _int(row.birth_month)}
            for row in players.itertuples()
        }
        names = dict(zip(players.player_id, players.player_name))

        # Rows stay in the export's stat_id order, which keeps tie-breaks in the
        # natural-bracket count identical to the scraper's.
        self.season_rows, self.history_rows = [], []
        all_rows = []
        for row in stats.itertuples():
            competition = self.competitions.get(row.competition_id, {})
            record = {
                "player_id": row.player_id,
                "player_name": names.get(row.player_id, ""),
                "season_id": int(row.season_id),
                "season": self.season_names.get(row.season_id, row.season_id),
                "team_id": row.team_id,
                "team_name": self.team_names.get(row.team_id, ""),
                "competition_id": row.competition_id,
                "league_name": competition.get("name", ""),
                "age_group": competition.get("age_group", ""),
                **{field: _int(getattr(row, field)) for field in SEASON_STAT_FIELDS},
            }
            all_rows.append(record)
            if any(record[field] is not None for field in SEASON_STAT_FIELDS):
                self.season_rows.append(record)
            else:
                self.history_rows.append(record)

        self._mark_above_age(all_rows)
        self.players = self._aggregate_players(players, all_rows)
        log.info(
            "loaded %s players, %s season rows with statistics, %s history-only rows",
            len(self.players),
            len(self.season_rows),
            len(self.history_rows),
        )

        matches = read_table(db_dir, "Player_Match_Stats", required=False)
        # An empty table means the weekly match import has not run yet, which the UI
        # reports as "coming soon" rather than "this player has no matches".
        self.match_rows = matches if matches is not None and len(matches) else None

    def _mark_above_age(self, rows):
        """Attach ``above_age_steps`` to every row, as ifa_scraper/run.py defines it.

        A player's natural bracket for a season is the youth bracket holding most of
        their birth-year cohort that season; the steps are how far above it they played.
        Rows outside the youth ladder, or without a birth year, get None.
        """
        counts = defaultdict(lambda: defaultdict(int))
        for row in rows:
            birth_year = self.details.get(row["player_id"], {}).get("birth_year")
            if birth_year and row["age_group"] in self.age_ladder:
                counts[(birth_year, row["season_id"])][row["age_group"]] += 1
        natural = {
            key: max(groups.items(), key=lambda item: item[1])[0]
            for key, groups in counts.items()
        }
        for row in rows:
            birth_year = self.details.get(row["player_id"], {}).get("birth_year")
            expected = natural.get((birth_year, row["season_id"]))
            if not birth_year or row["age_group"] not in self.age_ladder or expected is None:
                row["above_age_steps"] = None
            else:
                row["above_age_steps"] = self.age_ladder.index(row["age_group"]) - self.age_ladder.index(expected)

    def _aggregate_players(self, players, rows):
        """One record per player, with the fields players_youth.csv used to carry."""
        by_player = defaultdict(list)
        for row in rows:
            by_player[row["player_id"]].append(row)

        records = {}
        for player in players.itertuples():
            spells = sorted(
                by_player.get(player.player_id, []),
                key=lambda r: (-r["season_id"], r["team_name"]),
            )
            totals = defaultdict(int)
            for row in spells:
                for field in SEASON_STAT_FIELDS:
                    totals[field] += row[field] or 0

            def unique(values):
                seen = []
                for value in values:
                    if value and value not in seen:
                        seen.append(value)
                return seen

            teams = unique(row["team_name"] for row in spells)
            latest = spells[0]["season_id"] if spells else None
            above_now, above_history = 0, []
            for row in spells:
                steps = row["above_age_steps"]
                if steps is None or steps <= 0:
                    continue
                if row["season_id"] == latest:
                    above_now = max(above_now, steps)
                label = f"{row['age_group']} {row['season']} (+{steps})"
                if label not in above_history:
                    above_history.append(label)

            games = totals["games"]
            records[player.player_id] = {
                "player_id": player.player_id,
                "player_name": player.player_name,
                "birth_year": self.details[player.player_id]["birth_year"],
                "image_url": player.image_url,
                "current_team": teams[0] if teams else "",
                "num_teams": len(teams),
                "age_groups": ", ".join(unique(row["age_group"] for row in spells)),
                "leagues": ", ".join(unique(row["league_name"] for row in spells)),
                "seasons": ", ".join(unique(row["season"] for row in spells)),
                "plays_above_age": "Yes" if above_now > 0 else "No",
                "age_groups_above": above_now,
                "above_age_history": " | ".join(above_history),
                "goals_total": totals["goals"],
                # The schema keeps one row per player-team-season, with no league/cup
                # split of the goals, so these are unknown rather than zero.
                "goals_league": None,
                "goals_cup": None,
                "games_total": games,
                "minutes_total": totals["minutes"],
                "avg_minutes_per_game": _round_half_up(totals["minutes"] / games) if games else 0,
                "starts": totals["starts"],
                "sub_on": totals["sub_on"],
                "sub_off": totals["sub_off"],
                "yellow_cards_total": totals["yellow_cards_league_cup"] + totals["yellow_cards_toto"],
                "red_cards": totals["red_cards"],
            }
        return records
