"""PostgresScoutingData: query shaping and dataset-selection logic.

These tests never touch a real database -- they fake ``ifa_scraper.db.connect``
so the SQL-shaping and row-mapping code can be verified without psycopg
installed. The SQL text itself (column names, joins) was verified separately
by running the actual queries in this module against a real local PostgreSQL
16 instance built from the same schema as
``migrations/versions/0001_canonical_schema.py`` (see the implementation
report). What these tests cover is everything downstream of a successful
query: that raw rows are mapped into the exact shape
``ifa_scraper.run.aggregate_players`` and ``BaseScoutingData._normalise_stat_rows``
already expect (both already covered by their own CSV-path tests), and that
``PostgresScoutingData`` refuses to start when no dataset is published.
"""

import unittest
from unittest import mock

import pandas as pd

from dashboard.postgres_source import PostgresScoutingData

# Markers must not be substrings of one another's SQL text -- see the two
# "FROM players" queries below, which differ only by ", image_url".
PLAYER_SEASON_ROWS_MARKER = "FROM player_team_seasons pts"
PLAYER_DETAILS_WITH_IMAGE_MARKER = "birth_year, image_url FROM players"
PLAYER_BIRTH_YEAR_ONLY_MARKER = "SELECT player_id, birth_year FROM players"
TEAM_VENUE_MARKER = "FROM teams t"

_P1_MASKED_RECENT_ROW = (
    # player_id, player_name, season_id, season, team_id, team_name, age_group,
    # league_name, games, goals, minutes, starts, sub_on, sub_off,
    # yellow_cards_league_cup, yellow_cards_toto, red_cards -- no league_id here:
    # _load_players()'s catalog query never joins team_season_leagues, since
    # aggregate_players() does not read league_id at all.
    "p1", "*******", 26, "2024/25", "t1", "Hapoel Kiryat Gat",
    "נוער", "ליגת העל לנוער", 10, 2, 600, 8, 1, 2, 1, 0, 0,
)


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


class PostgresScoutingDataTests(unittest.TestCase):
    def _make(
        self,
        dataset_id=7,
        player_season_rows=(_P1_MASKED_RECENT_ROW,),
        player_details_rows=(("p1", 2011, ""),),
        player_birth_rows=(("p1", 2011),),
        team_venue_rows=(),
        history_frame=None,
    ):
        """Build a PostgresScoutingData with every DB call faked out."""
        connection = _FakeConnection(
            {
                PLAYER_SEASON_ROWS_MARKER: list(player_season_rows),
                PLAYER_DETAILS_WITH_IMAGE_MARKER: list(player_details_rows),
                PLAYER_BIRTH_YEAR_ONLY_MARKER: list(player_birth_rows),
                TEAM_VENUE_MARKER: list(team_venue_rows),
            }
        )
        season_frame = pd.DataFrame(
            [
                {
                    "player_id": "p1", "player_name": "*******", "season_id": 26,
                    "season": "2024/25", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                    "league_id": "101, 102", "age_group": "נוער", "league_name": "ליגת העל לנוער",
                    "games": 10, "goals": 2, "minutes": 600, "starts": 8, "sub_on": 1,
                    "sub_off": 2, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                }
            ]
        )
        if history_frame is None:
            history_frame = pd.DataFrame(
                [
                    {
                        "player_id": "p1", "player_name": "Yosef Cohen", "season_id": 25,
                        "season": "2023/24", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                        "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער",
                        "games": 5, "goals": 1, "minutes": 300, "starts": 4, "sub_on": 0,
                        "sub_off": 1, "yellow_cards_league_cup": 0, "yellow_cards_toto": 0,
                        "red_cards": 0, "stats_available": True,
                        "stats_source": "team_player_statistics", "stats_completeness": "full",
                    }
                ]
            )

        with mock.patch(
            "dashboard.postgres_source.db.connect", return_value=connection
        ), mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=dataset_id
        ), mock.patch.object(
            PostgresScoutingData,
            "_read_season_rows",
            side_effect=lambda source_file: (
                season_frame if source_file == "player_season_stats" else history_frame
            ),
        ):
            return PostgresScoutingData(database_url="postgresql://fake/fake")

    def test_no_live_dataset_raises(self):
        connection = _FakeConnection({})
        with mock.patch(
            "dashboard.postgres_source.db.connect", return_value=connection
        ), mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "no dataset is currently published"):
                PostgresScoutingData(database_url="postgresql://fake/fake")

    def test_load_players_uses_aggregate_players_over_season_stats_only(self):
        data = self._make()
        self.assertIn("p1", data.players)
        # The players catalog is built from player_season_stats rows only (like
        # players_youth.csv is), so it reflects only that source's own name --
        # the cross-source known-name recovery happens later, in player().
        self.assertEqual(data.players["p1"]["name_status"], "masked")

    def test_player_merge_recovers_known_name_from_history(self):
        data = self._make()
        player = data.player("p1")
        self.assertEqual(player["player_name"], "Yosef Cohen")
        season = next(s for s in player["seasons"] if s["season_id"] == 26)
        self.assertEqual(season["games"], 10)  # still the full-quality recent row's stats

    def test_load_locations_maps_team_to_its_current_venue(self):
        data = self._make(
            team_venue_rows=[
                ("t1", "Kiryat Gat Stadium", "Kiryat Gat", "", 31.61, 34.76, "street"),
            ]
        )
        self.assertEqual(data.locations["t1"]["city"], "Kiryat Gat")
        self.assertEqual(data.locations["t1"]["lat"], 31.61)

    def test_load_locations_omits_teams_without_a_current_venue(self):
        data = self._make(team_venue_rows=[])
        self.assertEqual(data.locations, {})

    def test_load_details_maps_birth_years(self):
        data = self._make(player_birth_rows=[("p1", 2011)])
        self.assertEqual(data.details["p1"], {"birth_year": 2011})


if __name__ == "__main__":
    unittest.main()
