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

from dashboard.postgres_source import _SEASON_ROW_SQL, PostgresScoutingData

# Markers must not be substrings of one another's SQL text -- see the two
# "FROM players" queries below, which differ only by ", image_url".
#
# This marker hits _load_players()'s own raw-cursor catalog query against
# player_team_season_observations (see dashboard/postgres_source.py's module
# docstring and the 0002 migration), NOT the pandas-based _SEASON_ROW_SQL
# used by _read_season_rows() -- that one is mocked out entirely below via
# mock.patch.object(..., "_read_season_rows", ...), so its own SQL text is
# never actually executed against these fakes.
PLAYER_SEASON_ROWS_MARKER = "FROM player_team_season_observations o"
PLAYER_DETAILS_WITH_IMAGE_MARKER = "birth_year, image_url FROM players"
PLAYER_BIRTH_YEAR_ONLY_MARKER = "SELECT player_id, birth_year FROM players"
# _load_locations() reads dataset_team_locations, NOT teams/venues (see the
# 0003 migration and dashboard/postgres_source.py's module docstring: the
# teams/venues join deduplicates by field_id, which silently drops a team's
# own city whenever it shares a field with another team). Column order is
# unchanged from the old teams/venues query, so existing fixture tuples below
# still apply verbatim.
TEAM_VENUE_MARKER = "FROM dataset_team_locations"

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
        self.executed_sql = []

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
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
        # One shared cursor rather than a fresh one per `with connection.cursor()`
        # block, so executed_sql accumulates across every query PostgresScoutingData
        # issues over this connection's lifetime -- needed to assert on SQL text
        # (e.g. an ORDER BY clause) issued from a block other tests don't otherwise
        # care about.
        self._cursor = _RoutingCursor(self.rows_by_marker)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class SeasonRowSqlTests(unittest.TestCase):
    def test_season_row_sql_orders_by_source_row_number(self):
        self.assertIn("ORDER BY o.source_row_number", _SEASON_ROW_SQL)


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

    def test_explicit_dataset_id_does_not_read_or_change_current_dataset(self):
        # Candidate parity must inspect an imported-but-not-yet-published
        # dataset without temporarily switching the production singleton.
        with mock.patch(
            "dashboard.postgres_source.db.current_dataset_id"
        ) as current_dataset_id, mock.patch.object(
            PostgresScoutingData, "_load_players", return_value={}
        ), mock.patch.object(
            PostgresScoutingData, "_load_season_rows", return_value=[]
        ), mock.patch.object(
            PostgresScoutingData, "_load_history_rows", return_value=[]
        ), mock.patch.object(
            PostgresScoutingData, "_load_locations", return_value={}
        ), mock.patch.object(
            PostgresScoutingData, "_load_details", return_value={}
        ):
            data = PostgresScoutingData(
                database_url="postgresql://fake/fake", dataset_id=8
            )

        self.assertEqual(data.dataset_id, 8)
        current_dataset_id.assert_not_called()

    def test_catalog_query_orders_by_source_row_number(self):
        # Smoke check that the ORDER BY survives edits to _load_players()'s
        # raw-cursor query text -- the fakes below never execute real SQL, so
        # this cannot catch a broken ORDER BY the way a real Postgres instance
        # would (see this module's own docstring on that limitation), but it
        # does catch someone deleting the clause outright. The actual
        # ordering behavior is exercised end-to-end with real data in
        # tests/test_row_order_and_locations.py.
        connection = _FakeConnection(
            {
                PLAYER_SEASON_ROWS_MARKER: [_P1_MASKED_RECENT_ROW],
                PLAYER_DETAILS_WITH_IMAGE_MARKER: [("p1", 2011, "")],
                PLAYER_BIRTH_YEAR_ONLY_MARKER: [("p1", 2011)],
                TEAM_VENUE_MARKER: [],
            }
        )
        minimal_frame = pd.DataFrame(
            [
                {
                    "player_id": "p1", "player_name": "*******", "season_id": 26,
                    "season": "2024/25", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                    "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער",
                    "games": 10, "goals": 2, "minutes": 600, "starts": 8, "sub_on": 1,
                    "sub_off": 2, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                }
            ]
        )
        with mock.patch(
            "dashboard.postgres_source.db.connect", return_value=connection
        ), mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=7
        ), mock.patch.object(
            PostgresScoutingData, "_read_season_rows", return_value=minimal_frame
        ):
            PostgresScoutingData(database_url="postgresql://fake/fake")

        catalog_sql = next(
            sql for sql in connection._cursor.executed_sql if PLAYER_SEASON_ROWS_MARKER in sql
        )
        self.assertIn("ORDER BY o.source_row_number", catalog_sql)

    def test_load_players_uses_aggregate_players_over_season_stats_only(self):
        data = self._make()
        self.assertIn("p1", data.players)
        # The players catalog is built from player_season_stats-source
        # observations only (like players_youth.csv is -- aggregate_players()
        # only ever runs over phase2_collect_squads's own output, see
        # ifa_scraper.run.main), so it reflects only that source's own name --
        # the cross-source known-name recovery happens later, in player().
        self.assertEqual(data.players["p1"]["name_status"], "masked")

    def test_player_merge_recovers_known_name_from_history(self):
        data = self._make()
        player = data.player("p1")
        self.assertEqual(player["player_name"], "Yosef Cohen")
        season = next(s for s in player["seasons"] if s["season_id"] == 26)
        self.assertEqual(season["games"], 10)  # still the full-quality recent row's stats

    def test_load_players_catalog_reflects_recent_source_only_not_merged_history(self):
        # A player_season_stats-only catalog must NOT pick up age_group/league
        # evidence that exists only in player_history.csv -- doing so was the
        # exact bug this table split fixes (see the 0002 migration's
        # docstring): it made historical age groups/leagues bleed into what
        # is supposed to be the *current* catalog.
        season_frame = pd.DataFrame(
            [
                {
                    "player_id": "p1", "player_name": "*******", "season_id": 26,
                    "season": "2024/25", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                    "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער",
                    "games": 10, "goals": 2, "minutes": 600, "starts": 8, "sub_on": 1,
                    "sub_off": 2, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                }
            ]
        )
        history_frame = pd.DataFrame(
            [
                {
                    "player_id": "p1", "player_name": "Yosef Cohen", "season_id": 25,
                    "season": "2023/24", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                    "league_id": "101, 102", "age_group": "נערים א", "league_name": "ליגה א, ליגה ב",
                    "games": 5, "goals": 1, "minutes": 300, "starts": 4, "sub_on": 0,
                    "sub_off": 1, "yellow_cards_league_cup": 0, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                },
                {
                    "player_id": "p2", "player_name": "מסארוה", "season_id": 25,
                    "season": "2023/24", "team_id": "t2", "team_name": "Maccabi Tel Aviv",
                    "league_id": "303", "age_group": "נוער", "league_name": "ליגה ב",
                    "games": 8, "goals": 3, "minutes": 420, "starts": 5, "sub_on": 1,
                    "sub_off": 0, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                },
            ]
        )

        with mock.patch("dashboard.postgres_source.db.connect") as connect, mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=7
        ), mock.patch.object(
            PostgresScoutingData,
            "_read_season_rows",
            side_effect=lambda source_file: season_frame if source_file == "player_season_stats" else history_frame,
        ):
            connect.return_value = _FakeConnection({
                PLAYER_SEASON_ROWS_MARKER: [
                    ("p1", "*******", 26, "2024/25", "t1", "Hapoel Kiryat Gat", "נוער", "ליגת העל לנוער", 10, 2, 600, 8, 1, 2, 1, 0, 0),
                ],
                PLAYER_DETAILS_WITH_IMAGE_MARKER: [("p1", 2011, ""), ("p2", 2009, "")],
                PLAYER_BIRTH_YEAR_ONLY_MARKER: [("p1", 2011), ("p2", 2009)],
                TEAM_VENUE_MARKER: [],
            })
            data = PostgresScoutingData(database_url="postgresql://fake/fake")

        # p2 has no player_season_stats-source observation at all, so (like a
        # player who never appears in players_youth.csv) it is not part of the
        # catalog -- it cannot be opened through this dashboard, matching
        # CsvScoutingData._load_history_rows()'s documented behavior.
        self.assertNotIn("p2", data.players)
        self.assertIn("נוער", data.players["p1"]["age_groups"])
        self.assertNotIn("נערים א", data.players["p1"]["age_groups"])
        self.assertIn("ליגת העל לנוער", data.players["p1"]["leagues"])
        self.assertNotIn("ליגה א", data.players["p1"]["leagues"])

    def test_search_excludes_players_with_no_recent_source_observation(self):
        # Mirrors CsvScoutingData: search_index is built from self.players,
        # which is itself built from player_season_stats-source rows only, so
        # a player who exists only in player_history.csv is not searchable --
        # exactly as documented on CsvScoutingData._load_history_rows().
        season_frame = pd.DataFrame(
            [
                {
                    "player_id": "p1", "player_name": "*******", "season_id": 26,
                    "season": "2024/25", "team_id": "t1", "team_name": "Hapoel Kiryat Gat",
                    "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער",
                    "games": 10, "goals": 2, "minutes": 600, "starts": 8, "sub_on": 1,
                    "sub_off": 2, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                }
            ]
        )
        history_frame = pd.DataFrame(
            [
                {
                    "player_id": "p2", "player_name": "מסארוה", "season_id": 25,
                    "season": "2023/24", "team_id": "t2", "team_name": "Maccabi Tel Aviv",
                    "league_id": "303", "age_group": "נוער", "league_name": "ליגה ב",
                    "games": 8, "goals": 3, "minutes": 420, "starts": 5, "sub_on": 1,
                    "sub_off": 0, "yellow_cards_league_cup": 1, "yellow_cards_toto": 0,
                    "red_cards": 0, "stats_available": True,
                    "stats_source": "team_player_statistics", "stats_completeness": "full",
                }
            ]
        )

        with mock.patch("dashboard.postgres_source.db.connect") as connect, mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=7
        ), mock.patch.object(
            PostgresScoutingData,
            "_read_season_rows",
            side_effect=lambda source_file: season_frame if source_file == "player_season_stats" else history_frame,
        ):
            connect.return_value = _FakeConnection(
                {
                    PLAYER_SEASON_ROWS_MARKER: [
                        ("p1", "*******", 26, "2024/25", "t1", "Hapoel Kiryat Gat", "נוער", "ליגת העל לנוער", 10, 2, 600, 8, 1, 2, 1, 0, 0),
                    ],
                    PLAYER_DETAILS_WITH_IMAGE_MARKER: [("p1", 2011, ""), ("p2", 2009, "")],
                    PLAYER_BIRTH_YEAR_ONLY_MARKER: [("p1", 2011), ("p2", 2009)],
                    TEAM_VENUE_MARKER: [],
                }
            )
            data = PostgresScoutingData(database_url="postgresql://fake/fake")

        ids = {entry["player_id"] for entry in data.search("מסארוה", limit=20)}
        self.assertNotIn("p2", ids)

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

    def test_load_locations_keeps_distinct_cities_for_teams_sharing_a_field(self):
        # dataset_team_locations is keyed by team_id, not field_id -- two
        # team_ids can (and in this repository's own data, do: field_id 48)
        # share a physical field but geocode to different real cities. Since
        # this query never groups by field, both must come back unmodified.
        data = self._make(
            team_venue_rows=[
                ("t1", "Shared Field", "Field City", "", 32.92, 35.25, "fallback"),
                ("t2", "Shared Field", "Own City", "", 32.83, 35.50, "locality"),
            ]
        )
        self.assertEqual(data.locations["t1"]["city"], "Field City")
        self.assertEqual(data.locations["t2"]["city"], "Own City")

    def test_load_details_maps_birth_years(self):
        data = self._make(player_birth_rows=[("p1", 2011)])
        self.assertEqual(data.details["p1"], {"birth_year": 2011})


if __name__ == "__main__":
    unittest.main()
