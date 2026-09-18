"""Regression: the observation-layer split (0002 migration) must not break
CSV/Postgres parity for known players, and source-specific field differences
must not be lost.

Uses real rows for player_id 206865 and 249469 -- the two players
``scripts.check_parity`` always checks in addition to its random sample --
extracted verbatim from this repository's own ``data/player_season_stats.csv``
and ``data/player_history.csv``. They happen to include a case where the two
sources disagree on team_name/age_group/league_name for the very same
(player, team, season) key: player_id 206865, team_id 1869, season_id 26 is
"מ.כ חולון ירמיהו" / "נוער" in the recent export but "מ.כ חולון ירמיהו \"צו
פיוס\"" in the historical export for the identical spell -- exactly the kind
of source-specific difference a merged-only read path would have erased.

These tests never touch a real database: the Postgres side fakes
``ifa_scraper.db.connect`` and ``PostgresScoutingData._read_season_rows`` with
data built from the same real rows, matching the convention already used in
``tests/test_postgres_source.py``.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from dashboard.app_core import CsvScoutingData
from dashboard.postgres_source import PostgresScoutingData
from ifa_scraper import run
from scripts.check_parity import compare_player

STAT_FIELDS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]

SEASON_STATS_COLUMNS = [
    "player_id", "player_name", "season_id", "season", "team_id", "team_name",
    "age_group", "league_name",
    *STAT_FIELDS,
]

HISTORY_COLUMNS = [
    "player_id", "player_name", "season_id", "season", "team_id", "team_name",
    "league_id", "age_group", "league_name",
    *STAT_FIELDS,
    "stats_available", "stats_source", "stats_completeness",
]

PLAYER_IDS = ["206865", "249469"]

# Real birth years for the two players, from data/player_details.csv. Needed
# so both sides compute the same age_groups_above/plays_above_age (which read
# birth_year) -- without these, both paths would fall back to "" vs None for
# an unknown birth_year in a way unrelated to anything this migration
# changes, and would spuriously fail the comparison below.
BIRTH_YEARS = {"206865": 2008, "249469": 2010}

# Verbatim rows for player_id 206865/249469 from data/player_season_stats.csv,
# which has no league_id/stats_available/stats_source/stats_completeness
# columns at all (confirmed against the real file's header).
SEASON_STATS_ROWS = [
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "26", "season": "2024/25", "team_id": "1641", "team_name": "מכבי ב\"ש 2", "age_group": "נערים ב", "league_name": "ליגת נערים ב' דרום", "games": "1", "goals": "0", "minutes": "74", "starts": "1", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "26", "season": "2024/25", "team_id": "1869", "team_name": "מ.כ חולון ירמיהו", "age_group": "נוער", "league_name": "ליגת נוער מרכז", "games": "3", "goals": "5", "minutes": "189", "starts": "2", "sub_on": "1", "sub_off": "1", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "27", "season": "2025/26", "team_id": "1869", "team_name": "מ.כ חולון ירמיהו", "age_group": "נוער", "league_name": "ליגת נוער מרכז", "games": "26", "goals": "85", "minutes": "2286", "starts": "26", "sub_on": "0", "sub_off": "8", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "26", "season": "2024/25", "team_id": "1887", "team_name": "מ.כ. חולון ירמיהו", "age_group": "נערים א", "league_name": "ליגת נערים א' מרכז", "games": "25", "goals": "45", "minutes": "2080", "starts": "24", "sub_on": "1", "sub_off": "8", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "1"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "27", "season": "2025/26", "team_id": "6504", "team_name": "מכבי קרית גת \"צו פיוס\"", "age_group": "נערים א", "league_name": "ליגת נערים א' ארצית דרום", "games": "6", "goals": "0", "minutes": "354", "starts": "2", "sub_on": "4", "sub_off": "1", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "27", "season": "2025/26", "team_id": "7437", "team_name": "מכבי קרית גת 2 \"צו פיוס\"", "age_group": "נערים ב", "league_name": "ליגת נערים ב' דרום", "games": "20", "goals": "4", "minutes": "1609", "starts": "20", "sub_on": "0", "sub_off": "3", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "26", "season": "2024/25", "team_id": "7454", "team_name": "מכבי ב\"ש 2", "age_group": "נערים ג", "league_name": "ליגת נערים ג' דרום", "games": "21", "goals": "3", "minutes": "1205", "starts": "14", "sub_on": "7", "sub_off": "6", "yellow_cards_league_cup": "5", "yellow_cards_toto": "0", "red_cards": "1"},
]

# Verbatim rows for the same two players from data/player_history.csv. Note
# the (206865, team_id=1869, season_id=26) row: same canonical key as the
# season_stats row above, but a different team_name/stats -- see the module
# docstring.
HISTORY_ROWS = [
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "28", "season": "2026/27", "team_id": "1869", "team_name": "מ.כ חולון ירמיהו \"צו פיוס\"", "league_id": "105", "age_group": "נוער", "league_name": "ליגה ארצית לנוער דרום", "games": "1", "goals": "0", "minutes": "61", "starts": "1", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "27", "season": "2025/26", "team_id": "1869", "team_name": "מ.כ חולון ירמיהו \"צו פיוס\"", "league_id": "666", "age_group": "נוער", "league_name": "ליגת נוער מרכז", "games": "26", "goals": "85", "minutes": "2286", "starts": "26", "sub_on": "0", "sub_off": "8", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "26", "season": "2024/25", "team_id": "1869", "team_name": "מ.כ חולון ירמיהו \"צו פיוס\"", "league_id": "666", "age_group": "נוער", "league_name": "ליגת נוער מרכז", "games": "3", "goals": "5", "minutes": "189", "starts": "2", "sub_on": "1", "sub_off": "1", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "26", "season": "2024/25", "team_id": "1887", "team_name": "מ.כ. חולון ירמיהו \"צו פיוס\"", "league_id": "123", "age_group": "נערים א", "league_name": "ליגת נערים א' מרכז", "games": "25", "goals": "45", "minutes": "2080", "starts": "24", "sub_on": "1", "sub_off": "8", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "1", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "25", "season": "2023/24", "team_id": "5758", "team_name": "מכבי ע. בת ים משה נהרדע  \"צו פיוס\"", "league_id": "137", "age_group": "נערים ב", "league_name": "ליגת נערים ב' מרכז", "games": "25", "goals": "15", "minutes": "1743", "starts": "20", "sub_on": "5", "sub_off": "4", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "24", "season": "2022/23", "team_id": "4028", "team_name": "בית\"ר תל אביב חולון \"צו פיוס\"", "league_id": "137", "age_group": "נערים ב", "league_name": "ליגת נערים ב' מרכז", "games": "6", "goals": "1", "minutes": "328", "starts": "3", "sub_on": "3", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "24", "season": "2022/23", "team_id": "4236", "team_name": "בית\"ר תל אביב חולון 2 \"צו פיוס\"", "league_id": "144", "age_group": "נערים ג", "league_name": "ליגת נערים ג' שרון", "games": "23", "goals": "13", "minutes": "1851", "starts": "22", "sub_on": "1", "sub_off": "5", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "23", "season": "2021/22", "team_id": "4031", "team_name": "בית\"ר תל אביב חולון 1 \"צו פיוס\"", "league_id": "734", "age_group": "ילדים א", "league_name": "ליגת ילדים א' דן 1", "games": "25", "goals": "24", "minutes": "1608", "starts": "24", "sub_on": "1", "sub_off": "11", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "22", "season": "2020/21", "team_id": "4034", "team_name": "בית\"ר תל אביב חולון", "league_id": "791", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' דן 3", "games": "0", "goals": "0", "minutes": "0", "starts": "0", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "21", "season": "2019/20", "team_id": "4214", "team_name": "בית\"ר תל אביב חולון \"צו פיוס\"", "league_id": "175", "age_group": "ילדים ג", "league_name": "ליגת ילדים ג' דן", "games": "0", "goals": "0", "minutes": "0", "starts": "0", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206865", "player_name": "בן ניסן אליה", "season_id": "20", "season": "2018/19", "team_id": "5763", "team_name": "מכבי ע. בת ים דרום \"צו פיוס\"", "league_id": "631", "age_group": "ילדים טרום א", "league_name": "ליגת ילדים טרום א' דן 1", "games": "27", "goals": "0", "minutes": "1890", "starts": "27", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "28", "season": "2026/27", "team_id": "6504", "team_name": "מכבי קרית גת \"צו פיוס\"", "league_id": "121", "age_group": "נערים א", "league_name": "נערים א' ארצית דרום", "games": "2", "goals": "0", "minutes": "143", "starts": "2", "sub_on": "0", "sub_off": "2", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "27", "season": "2025/26", "team_id": "6504", "team_name": "מכבי קרית גת \"צו פיוס\"", "league_id": "121", "age_group": "נערים א", "league_name": "ליגת נערים א' ארצית דרום", "games": "6", "goals": "0", "minutes": "354", "starts": "2", "sub_on": "4", "sub_off": "1", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "27", "season": "2025/26", "team_id": "7437", "team_name": "מכבי קרית גת 2 \"צו פיוס\"", "league_id": "139", "age_group": "נערים ב", "league_name": "ליגת נערים ב' דרום", "games": "20", "goals": "4", "minutes": "1609", "starts": "20", "sub_on": "0", "sub_off": "3", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "26", "season": "2024/25", "team_id": "1641", "team_name": "מכבי ב\"ש 2 \"צו פיוס\"", "league_id": "139", "age_group": "נערים ב", "league_name": "ליגת נערים ב' דרום", "games": "1", "goals": "0", "minutes": "74", "starts": "1", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "26", "season": "2024/25", "team_id": "7454", "team_name": "מכבי ב\"ש 2 \"צו פיוס\"", "league_id": "663", "age_group": "נערים ג", "league_name": "ליגת נערים ג' דרום", "games": "21", "goals": "3", "minutes": "1205", "starts": "14", "sub_on": "7", "sub_off": "6", "yellow_cards_league_cup": "5", "yellow_cards_toto": "0", "red_cards": "1", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "25", "season": "2023/24", "team_id": "1873", "team_name": "מכבי ב\"ש צפון \"צו פיוס\"", "league_id": "827", "age_group": "ילדים א", "league_name": "ליגת ילדים א' דרג 1", "games": "10", "goals": "1", "minutes": "174", "starts": "1", "sub_on": "9", "sub_off": "2", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "24", "season": "2022/23", "team_id": "7455", "team_name": "מכבי ב\"ש 2 \"צו פיוס\"", "league_id": "739", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' דן", "games": "24", "goals": "0", "minutes": "1755", "starts": "24", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "249469", "player_name": "אבוחצירה רואי", "season_id": "24", "season": "2022/23", "team_id": "1873", "team_name": "מכבי ב\"ש צפון \"צו פיוס\"", "league_id": "156", "age_group": "ילדים א", "league_name": "ליגת ילדים א' מרכז", "games": "2", "goals": "0", "minutes": "53", "starts": "0", "sub_on": "2", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
]


def _write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class _RoutingCursor:
    """Returns whichever registered rows match a substring of the executed SQL."""

    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self._rows = []

    def execute(self, sql, params=None):
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                self._rows = rows
                return
        raise AssertionError(f"no fake rows registered for query: {sql[:80]}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker

    def cursor(self):
        return _RoutingCursor(self.rows_by_marker)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class ObservationLayerRealDataParityTests(unittest.TestCase):
    """Prove the split introduced by the 0002 migration preserves real parity."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

        _write_csv(self.data_dir / "player_season_stats.csv", SEASON_STATS_ROWS, SEASON_STATS_COLUMNS)
        _write_csv(self.data_dir / "player_history.csv", HISTORY_ROWS, HISTORY_COLUMNS)
        _write_csv(
            self.data_dir / "player_details.csv",
            [{"player_id": pid, "birth_year": str(year), "image_url": ""} for pid, year in BIRTH_YEARS.items()],
            ["player_id", "birth_year", "image_url"],
        )

        details = {pid: {"birth_year": year} for pid, year in BIRTH_YEARS.items()}

        # players_youth.csv must be built the same way the real scraper builds
        # it -- aggregate_players() over the recent scrape's own season rows
        # only (ifa_scraper.run.main), never over player_history.csv -- so the
        # CSV-side catalog is exactly what PostgresScoutingData._load_players
        # is now expected to reproduce from observations.
        season_rows_for_aggregate = [
            {**row, "season_id": int(row["season_id"]), **{f: int(row[f]) for f in STAT_FIELDS}}
            for row in SEASON_STATS_ROWS
        ]
        players = run.aggregate_players(season_rows_for_aggregate, splits={}, details=details)
        run.write_csv(self.data_dir / "players_youth.csv", run.PLAYER_COLUMNS, players)

        self.csv_data = CsvScoutingData(self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def _season_frame(self):
        rows = []
        for row in SEASON_STATS_ROWS:
            rows.append(
                {
                    "player_id": row["player_id"], "player_name": row["player_name"],
                    "season_id": int(row["season_id"]), "season": row["season"],
                    "team_id": row["team_id"], "team_name": row["team_name"],
                    "league_id": "", "age_group": row["age_group"], "league_name": row["league_name"],
                    **{field: int(row[field]) for field in STAT_FIELDS},
                    # player_season_stats.csv has no stats_* columns at all;
                    # observations must record that as NULL (see the 0002
                    # migration), not a coerced False/"unavailable".
                    "stats_available": None, "stats_source": None, "stats_completeness": None,
                }
            )
        return pd.DataFrame(rows)

    def _history_frame(self):
        rows = []
        for row in HISTORY_ROWS:
            rows.append(
                {
                    "player_id": row["player_id"], "player_name": row["player_name"],
                    "season_id": int(row["season_id"]), "season": row["season"],
                    "team_id": row["team_id"], "team_name": row["team_name"],
                    "league_id": row["league_id"], "age_group": row["age_group"], "league_name": row["league_name"],
                    **{field: int(row[field]) for field in STAT_FIELDS},
                    "stats_available": row["stats_available"] == "True",
                    "stats_source": row["stats_source"], "stats_completeness": row["stats_completeness"],
                }
            )
        return pd.DataFrame(rows)

    def _make_pg_data(self):
        season_frame = self._season_frame()
        history_frame = self._history_frame()

        season_catalog_rows = [
            (
                row["player_id"], row["player_name"], int(row["season_id"]), row["season"],
                row["team_id"], row["team_name"], row["age_group"], row["league_name"],
                *[int(row[field]) for field in STAT_FIELDS],
            )
            for row in SEASON_STATS_ROWS
        ]
        connection = _FakeConnection(
            {
                "FROM player_team_season_observations o": season_catalog_rows,
                "birth_year, image_url FROM players": [
                    (pid, year, "") for pid, year in BIRTH_YEARS.items()
                ],
                "SELECT player_id, birth_year FROM players": [
                    (pid, year) for pid, year in BIRTH_YEARS.items()
                ],
                "FROM teams t": [],
            }
        )
        with mock.patch(
            "dashboard.postgres_source.db.connect", return_value=connection
        ), mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=1
        ), mock.patch.object(
            PostgresScoutingData,
            "_read_season_rows",
            side_effect=lambda source_file: (
                season_frame if source_file == "player_season_stats" else history_frame
            ),
        ):
            return PostgresScoutingData(database_url="postgresql://fake/fake")

    def test_known_players_retain_parity_after_observation_layer_split(self):
        pg_data = self._make_pg_data()
        mismatches = []
        for player_id in PLAYER_IDS:
            compare_player(self.csv_data, pg_data, player_id, mismatches)
        self.assertEqual(mismatches, [])

    def test_both_source_observations_remain_independently_queryable(self):
        # (player_id=206865, team_id=1869, season_id=26) is the same canonical
        # key in both sources but with a different team_name -- proving both
        # rows survive as separate observations, not just whichever one won
        # the canonical merge.
        pg_data = self._make_pg_data()
        recent_row = next(
            r for r in pg_data.rows_by_player["206865"]
            if r["season_id"] == 26 and r["team_id"] == "1869"
        )
        history_row = next(
            r for r in pg_data.history_by_player["206865"]
            if r["season_id"] == 26 and r["team_id"] == "1869"
        )
        self.assertEqual(recent_row["team_name"], "מ.כ חולון ירמיהו")
        self.assertEqual(history_row["team_name"], "מ.כ חולון ירמיהו \"צו פיוס\"")
        self.assertNotEqual(recent_row["team_name"], history_row["team_name"])

        # And this is exactly what CsvScoutingData already does -- the
        # observation layer must match it, not merge it away.
        csv_recent = next(
            r for r in self.csv_data.rows_by_player["206865"]
            if r["season_id"] == 26 and r["team_id"] == "1869"
        )
        csv_history = next(
            r for r in self.csv_data.history_by_player["206865"]
            if r["season_id"] == 26 and r["team_id"] == "1869"
        )
        self.assertEqual(recent_row["team_name"], csv_recent["team_name"])
        self.assertEqual(history_row["team_name"], csv_history["team_name"])

    def test_age_groups_leagues_current_team_share_the_csv_source_population(self):
        pg_data = self._make_pg_data()
        for player_id in PLAYER_IDS:
            csv_player = self.csv_data.player(player_id)
            pg_player = pg_data.player(player_id)
            self.assertEqual(pg_player["age_groups"], csv_player["age_groups"], player_id)
            self.assertEqual(pg_player["leagues"], csv_player["leagues"], player_id)
            self.assertEqual(pg_player["current_team"], csv_player["current_team"], player_id)


if __name__ == "__main__":
    unittest.main()
