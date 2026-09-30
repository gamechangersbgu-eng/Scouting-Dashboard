"""Pure-Python parts of scripts.import_canonical_dataset -- no database needed.

The parts of the importer that actually talk to Postgres (dataset creation,
upserts, the fact-row insert) are exercised against a real PostgreSQL 16
instance manually -- see the implementation report -- rather than mocked
here, since faithfully mocking psycopg's executemany/cursor semantics would
mostly test the mock. What is unit-tested here is the CSV-shaping logic that
runs before any SQL is issued: natural-key de-duplication, and the value
coercion helpers that turn empty CSV cells into ``None`` rather than ``""``
or ``0`` (an empty stat must stay unknown, not become a false zero).
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.app_core import merge_canonical_rows
from ifa_scraper.validation import ValidationResult
from scripts.check_parity import run_parity_check
from scripts.import_canonical_dataset import (
    _bool_from_csv,
    _insert_facts,
    _insert_observations,
    _int_or_none,
    _load_season_source,
    _load_venue_sources,
    _optional_bool,
    _set_local_statement_timeout,
    _text_or_none,
    _upsert_dataset_team_locations,
    _upsert_leagues_and_memberships,
    _upsert_players,
    _validate_imported_dataset,
)


class CoercionHelperTests(unittest.TestCase):
    def test_int_or_none(self):
        self.assertEqual(_int_or_none("12"), 12)
        self.assertIsNone(_int_or_none(""))
        self.assertIsNone(_int_or_none(None))
        self.assertIsNone(_int_or_none("not a number"))

    def test_text_or_none(self):
        self.assertEqual(_text_or_none(" Hapoel "), "Hapoel")
        self.assertIsNone(_text_or_none(""))
        self.assertIsNone(_text_or_none(None))

    def test_bool_from_csv(self):
        self.assertTrue(_bool_from_csv("true"))
        self.assertTrue(_bool_from_csv("Yes"))
        self.assertFalse(_bool_from_csv("false"))
        self.assertFalse(_bool_from_csv(""))
        self.assertTrue(_bool_from_csv("", default=True))


class LoadSeasonSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, header, rows):
        import csv

        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def test_duplicate_natural_key_collapses_keeping_the_later_row(self):
        header = ["player_id", "team_id", "season_id", "player_name", "games"]
        self._write(
            "dup.csv",
            header,
            [
                ["p1", "t1", "26", "Old Row", "3"],
                ["p1", "t1", "26", "Newer Row", "5"],  # same natural key
                ["p2", "t1", "26", "Other Player", "1"],
            ],
        )
        rows = _load_season_source(self.data_dir / "dup.csv", "player_season_stats")
        self.assertEqual(len(rows), 2)
        p1_row = next(r for r in rows if r["player_id"] == "p1")
        self.assertEqual(p1_row["player_name"], "Newer Row")
        self.assertEqual(p1_row["_source_file"], "player_season_stats")

    def test_missing_file_returns_no_rows(self):
        rows = _load_season_source(self.data_dir / "missing.csv", "player_history")
        self.assertEqual(rows, [])


class LoadVenueSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, header, rows):
        import csv

        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def test_team_fields_and_geocoding_join_by_field_id(self):
        self._write(
            "team_fields.csv",
            ["team_id", "team_name", "field_id", "field_name", "num_fields"],
            [
                ["t1", "Hapoel Kiryat Gat", "f1", "Kiryat Gat Stadium", "1"],
                ["t2", "No Geocode FC", "f2", "Some Field", "1"],
            ],
        )
        self._write(
            "team_locations.csv",
            ["team_id", "team_name", "field_id", "field_name", "address", "city", "district", "lat", "lon", "geocode_query", "precision"],
            [
                ["t1", "Hapoel Kiryat Gat", "f1", "Kiryat Gat Stadium", "Some St", "Kiryat Gat", "South", "31.61", "34.76", "q", "street"],
            ],
        )
        team_fields, venues_by_field = _load_venue_sources(self.data_dir)
        self.assertEqual(team_fields["t1"]["field_id"], "f1")
        self.assertEqual(team_fields["t2"]["field_id"], "f2")
        self.assertEqual(venues_by_field["f1"]["city"], "Kiryat Gat")
        self.assertEqual(venues_by_field["f1"]["lat"], 31.61)
        # t2's field was never geocoded, so it simply has no enrichment entry --
        # the importer falls back to field_name-only, lat/lon left NULL.
        self.assertNotIn("f2", venues_by_field)


class _FakeCursor:
    """Records every execute() call instead of talking to a real database."""

    def __init__(self):
        self.calls = []
        self.executemany_calls = []
        self.fetchall_result = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        if sql.startswith("SELECT player_id, name_status FROM players"):
            self._last_select = self.fetchall_result

    def executemany(self, sql, seq_of_params):
        self.executemany_calls.append((" ".join(sql.split()), list(seq_of_params)))
        for params in seq_of_params:
            self.execute(sql, params)

    def fetchall(self):
        return self.fetchall_result


class _ValidationCursor:
    """Minimal cursor for the post-build validation transaction."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        self.connection.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.connection.previous_dataset

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _ValidationConnection:
    def __init__(self, previous_dataset=(41,)):
        self.previous_dataset = previous_dataset
        self.calls = []

    def cursor(self):
        return _ValidationCursor(self)


def _leagues_upserted(cursor):
    return {params[0]: params[1] for sql, params in cursor.calls if sql.startswith("INSERT INTO leagues")}


def _memberships_inserted(cursor):
    # params = (dataset_id, team_id, season_id, league_id)
    return {
        (params[0], params[1], params[2])
        for sql, params in cursor.calls
        if sql.startswith("INSERT INTO team_season_leagues")
    }


class UpsertLeaguesAndMembershipsTests(unittest.TestCase):
    def test_single_valued_row_resolves_the_real_league_name(self):
        rows = [
            {"team_id": "t1", "season_id": "26", "league_id": "101", "league_name": "Top League"},
        ]
        cursor = _FakeCursor()
        _upsert_leagues_and_memberships(cursor, dataset_id=1, all_rows=rows)
        self.assertEqual(_leagues_upserted(cursor), {101: "Top League"})
        self.assertEqual(_memberships_inserted(cursor), {(1, "t1", 26)})

    def test_multi_valued_row_records_membership_without_guessing_a_pairing(self):
        # This is the real repository example (player_id 151674, team_id 5613,
        # season_id 16): league_id "182, 740" with two names that must not be
        # asserted as paired to a specific id from this row alone.
        rows = [
            {
                "team_id": "5613", "season_id": "16", "league_id": "182, 740",
                "league_name": "Tornir Merkaz, Liga Dan",
            },
        ]
        cursor = _FakeCursor()
        _upsert_leagues_and_memberships(cursor, dataset_id=1, all_rows=rows)
        leagues = _leagues_upserted(cursor)
        # Never seen single-valued anywhere -> placeholder name, not a guess.
        self.assertEqual(leagues, {182: "League 182", 740: "League 740"})
        # Both league_ids are recorded as memberships of the same team-season.
        membership_rows = [params for sql, params in cursor.calls if sql.startswith("INSERT INTO team_season_leagues")]
        self.assertEqual({p[3] for p in membership_rows}, {182, 740})

    def test_multi_valued_row_does_not_overwrite_a_name_learned_elsewhere(self):
        # league_id 182 is resolved unambiguously by a single-valued row for a
        # different team-season; the multi-valued row must not clobber it.
        rows = [
            {"team_id": "t1", "season_id": "25", "league_id": "182", "league_name": "Real Name For 182"},
            {"team_id": "5613", "season_id": "16", "league_id": "182, 740", "league_name": "X, Y"},
        ]
        cursor = _FakeCursor()
        _upsert_leagues_and_memberships(cursor, dataset_id=1, all_rows=rows)
        leagues = _leagues_upserted(cursor)
        self.assertEqual(leagues[182], "Real Name For 182")
        self.assertEqual(leagues[740], "League 740")


class BulkPlayerUpsertTests(unittest.TestCase):
    def test_local_statement_timeout_is_set_before_bulk_import_work(self):
        cursor = _FakeCursor()
        _set_local_statement_timeout(cursor)
        self.assertEqual(cursor.calls[0][0], "SET LOCAL statement_timeout = '30min'")

    def test_known_name_invariant_is_preserved_with_bulk_updates(self):
        cursor = _FakeCursor()
        cursor.fetchall_result = [("p1", "known")]
        data_dir = Path(tempfile.mkdtemp())
        (data_dir / "player_details.csv").write_text(
            "player_id,birth_year,image_url\np1,2000,https://example.com/p1.jpg\n",
            encoding="utf-8",
        )

        _upsert_players(
            cursor,
            [{"player_id": "p1", "player_name": "*******"}],
            data_dir,
        )

        self.assertTrue(cursor.executemany_calls)
        update_calls = [c for c in cursor.executemany_calls if c[0].startswith("UPDATE players SET")]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1][0][2], "p1")
        self.assertEqual(update_calls[0][1][0][0], 2000)


class ValidationTransactionTimeoutTests(unittest.TestCase):
    def test_validation_connection_sets_local_timeout_before_validation(self):
        """The post-commit connection needs its own transaction-local timeout."""
        connection = _ValidationConnection(previous_dataset=(41,))
        expected_result = ValidationResult(ok=True)

        def validate(connection_received, dataset_id, previous_dataset_id):
            self.assertIs(connection_received, connection)
            self.assertEqual(dataset_id, 42)
            self.assertEqual(previous_dataset_id, 41)
            self.assertEqual(
                connection.calls[0][0],
                "SET LOCAL statement_timeout = '30min'",
            )
            return expected_result

        with patch(
            "scripts.dataset_validation.validation.validate_dataset",
            side_effect=validate,
        ):
            result = _validate_imported_dataset(connection, dataset_id=42)

        self.assertIs(result, expected_result)
        self.assertEqual(
            connection.calls[1][0],
            "SELECT status FROM dataset_versions WHERE dataset_id = %s",
        )
        self.assertEqual(
            connection.calls[2][0],
            "SELECT dataset_id FROM current_dataset WHERE id",
        )
        self.assertTrue(connection.calls[3][0].startswith("UPDATE dataset_versions SET status"))


class CheckParityPathHandingTests(unittest.TestCase):
    def test_run_parity_check_accepts_cli_string_data_dir(self):
        seen = {}

        class DummyCsvData:
            def __init__(self, data_dir):
                seen["data_dir"] = data_dir
                self.data_dir = data_dir
                self.players = {"1": {"player_name": "Alice"}}

            def player(self, _player_id):
                return {"player_name": "Alice", "birth_year": None, "current_team": None, "num_teams": 0, "age_groups": [], "leagues": [], "seasons_played": 0, "plays_above_age": False, "age_groups_above": [], "first_club": None, "first_club_city": None, "likely_origin_city": None, "likely_origin_confidence": None, "totals": {}, "seasons": []}

            def summary(self):
                return {"players": 1, "player_seasons": 0, "above_age": 0}

            def search(self, query, limit=1000):
                return [{"player_id": "1"}]

        class DummyPgData:
            def __init__(self, database_url):
                self.database_url = database_url

            def player(self, _player_id):
                return {"player_name": "Alice", "birth_year": None, "current_team": None, "num_teams": 0, "age_groups": [], "leagues": [], "seasons_played": 0, "plays_above_age": False, "age_groups_above": [], "first_club": None, "first_club_city": None, "likely_origin_city": None, "likely_origin_confidence": None, "totals": {}, "seasons": []}

            def summary(self):
                return {"players": 1, "player_seasons": 0, "above_age": 0}

            def search(self, query, limit=1000):
                return [{"player_id": "1"}]

        with patch("scripts.check_parity.CsvScoutingData", DummyCsvData), patch("dashboard.postgres_source.PostgresScoutingData", DummyPgData):
            mismatches, sample_size = run_parity_check(data_dir=str(Path(tempfile.mkdtemp())), database_url="postgresql://example", sample_size=1, seed=0)
            self.assertEqual(mismatches, [])
            self.assertIsInstance(seen["data_dir"], Path)
            self.assertEqual(sample_size, 3)


class MergeBeforeInsertTests(unittest.TestCase):
    """The importer must insert at most one row per (player, team, season)."""

    def test_two_sources_for_the_same_grain_produce_one_inserted_row(self):
        history_row = {
            "player_id": "151674", "team_id": "5613", "season_id": "16",
            "player_name": "Real Name", "team_name": "Club", "league_name": "League",
            "age_group": "Youth", "stats_available": "true", "stats_completeness": "full",
            "games": "14", "goals": "0", "minutes": "560", "starts": "14",
            "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0",
            "yellow_cards_toto": "0", "red_cards": "0",
            "_source_file": "player_history",
        }
        season_row = dict(history_row, player_name="*******", _source_file="player_season_stats")

        merged_rows = list(merge_canonical_rows([history_row, season_row]).values())
        self.assertEqual(len(merged_rows), 1)

        cursor = _FakeCursor()
        count = _insert_facts(cursor, dataset_id=1, merged_rows=merged_rows)
        self.assertEqual(count, 1)
        insert_calls = [c for c in cursor.calls if c[0].startswith("INSERT INTO player_team_seasons")]
        self.assertEqual(len(insert_calls), 1)
        # The masked recent row is preferred on stats (a tie here), but the
        # known name from history must still be what gets inserted.
        self.assertEqual(insert_calls[0][1][5], "Real Name")


class InsertObservationsTests(unittest.TestCase):
    """The lossless observation layer (0002 migration) must never lose a source's row.

    ``player_team_seasons`` merges overlapping keys down to one canonical row
    (see MergeBeforeInsertTests above) -- that is correct for the canonical
    table, but it is exactly the information ``player_team_season_observations``
    exists to preserve for the two sources independently. These tests prove
    ``_insert_observations`` never performs that merge.
    """

    def _observation_rows(self, cursor):
        return [
            params
            for sql, params in cursor.calls
            if sql.startswith("INSERT INTO player_team_season_observations")
        ]

    def test_overlapping_canonical_key_still_produces_one_observation_row_per_source(self):
        # Same (player_id, team_id, season_id) as MergeBeforeInsertTests above,
        # where merge_canonical_rows() collapses these two rows into one
        # canonical player_team_seasons row -- source-specific team_name/
        # age_group/league_name must not be lost just because the canonical
        # merge discards one of them.
        history_row = {
            "player_id": "151674", "team_id": "5613", "season_id": "16",
            "player_name": "Real Name", "team_name": "Historical Club Name",
            "league_name": "Historical League", "age_group": "Youth (history)",
            "stats_available": "true", "stats_completeness": "full",
            "games": "14", "goals": "0", "minutes": "560", "starts": "14",
            "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0",
            "yellow_cards_toto": "0", "red_cards": "0",
            "_source_file": "player_history",
        }
        season_row = {
            **history_row,
            "player_name": "*******", "team_name": "Recent Club Name",
            "league_name": "Recent League", "age_group": "Youth (recent)",
            "_source_file": "player_season_stats",
        }
        all_rows = [history_row, season_row]

        # The canonical merge still collapses these to one row (unaffected by
        # this change) ...
        merged_rows = list(merge_canonical_rows(all_rows).values())
        self.assertEqual(len(merged_rows), 1)

        # ... but both source observations remain queryable independently.
        cursor = _FakeCursor()
        count = _insert_observations(cursor, dataset_id=1, all_rows=all_rows)
        self.assertEqual(count, 2)
        rows = self._observation_rows(cursor)
        self.assertEqual(len(rows), 2)

        by_source = {row[4]: row for row in rows}  # index 4 = source_file
        self.assertEqual(set(by_source), {"player_history", "player_season_stats"})
        # team_name (index 6), age_group (index 7), league_name (index 8) each
        # keep their own source's value rather than the merge winner's.
        self.assertEqual(by_source["player_history"][6], "Historical Club Name")
        self.assertEqual(by_source["player_history"][7], "Youth (history)")
        self.assertEqual(by_source["player_history"][8], "Historical League")
        self.assertEqual(by_source["player_season_stats"][6], "Recent Club Name")
        self.assertEqual(by_source["player_season_stats"][7], "Youth (recent)")
        self.assertEqual(by_source["player_season_stats"][8], "Recent League")

    def test_full_overlap_between_sources_still_inserts_every_raw_row(self):
        # Structurally the same property the real repository data exhibits:
        # 49,593 recent-source rows and 194,518 history rows, almost entirely
        # overlapping on canonical key (49,379 of them), yet
        # player_team_season_observations must hold all of them, not the
        # smaller merged count. Reproduced here at a scale a unit test can
        # run quickly, with every key overlapping (the worst case).
        history_rows = [
            {
                "player_id": f"p{i}", "team_id": "t1", "season_id": "10",
                "player_name": f"History Name {i}", "team_name": "Club",
                "league_name": "League", "age_group": "Youth",
                "stats_available": "true", "stats_completeness": "full",
                "games": "1", "goals": "0", "minutes": "10", "starts": "1",
                "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0",
                "yellow_cards_toto": "0", "red_cards": "0",
                "_source_file": "player_history",
            }
            for i in range(25)
        ]
        season_rows = [dict(row, _source_file="player_season_stats") for row in history_rows]
        all_rows = history_rows + season_rows

        merged_rows = list(merge_canonical_rows(all_rows).values())
        self.assertEqual(len(merged_rows), 25)  # fully collapsed, one per player

        cursor = _FakeCursor()
        count = _insert_observations(cursor, dataset_id=1, all_rows=all_rows)
        self.assertEqual(count, 50)  # nothing merged away at the observation layer
        self.assertEqual(len(self._observation_rows(cursor)), 50)

    def test_recent_source_missing_stats_columns_are_stored_as_null_not_false(self):
        # player_season_stats.csv genuinely has no stats_available/stats_source/
        # stats_completeness columns at all (confirmed against this repository's
        # own data/player_season_stats.csv header) -- _load_season_source()
        # therefore hands _insert_observations a row with those keys entirely
        # absent, not empty-string. A lossless observation must record that as
        # SQL NULL so _normalise_stat_rows can still infer it correctly at read
        # time, not silently become stats_available=False/"unavailable".
        season_row = {
            "player_id": "p1", "team_id": "t1", "season_id": "10",
            "player_name": "Name", "team_name": "Club", "league_name": "League",
            "age_group": "Youth", "games": "5", "goals": "1", "minutes": "300",
            "starts": "4", "sub_on": "0", "sub_off": "1",
            "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0",
            "_source_file": "player_season_stats",
            # stats_available / stats_source / stats_completeness: absent.
        }
        cursor = _FakeCursor()
        _insert_observations(cursor, dataset_id=1, all_rows=[season_row])
        (row,) = self._observation_rows(cursor)
        # dataset_id, player_id, team_id, season_id, source_file, player_name,
        # team_name, age_group, league_name, games..red_cards (9 fields),
        # stats_available, stats_source, stats_completeness, source_row_number
        stats_available, stats_source, stats_completeness = row[18:21]
        self.assertIsNone(stats_available)
        self.assertIsNone(stats_source)
        self.assertIsNone(stats_completeness)

    def test_optional_bool_leaves_absent_value_unknown(self):
        self.assertIsNone(_optional_bool(""))
        self.assertIsNone(_optional_bool(None))
        self.assertTrue(_optional_bool("true"))
        self.assertFalse(_optional_bool("false"))

    def test_source_row_number_is_written_as_the_last_column(self):
        # See the 0003 migration: source_row_number is what lets
        # PostgresScoutingData reproduce CsvScoutingData's CSV-file-order
        # tie-break (BaseScoutingData.player()'s stable sort by
        # (season_id, team_name) falls through to it when two rows share both).
        row = {
            "player_id": "p1", "team_id": "t1", "season_id": "10",
            "player_name": "Name", "team_name": "Club", "league_name": "League",
            "age_group": "Youth", "games": "5", "goals": "1", "minutes": "300",
            "starts": "4", "sub_on": "0", "sub_off": "1",
            "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0",
            "_source_file": "player_season_stats", "_source_row_number": 7,
        }
        cursor = _FakeCursor()
        _insert_observations(cursor, dataset_id=1, all_rows=[row])
        (observation_row,) = self._observation_rows(cursor)
        self.assertEqual(observation_row[-1], 7)


class LoadSeasonSourceRowNumberTests(unittest.TestCase):
    """_load_season_source() must record each surviving row's file position.

    This is the only lossless way to reproduce CsvScoutingData's CSV-file-order
    tie-break on the Postgres side -- see the 0003 migration's docstring, and
    tests/test_row_order_and_locations.py for the end-to-end regression using
    real repository data (player_id 160443, 206511, 227243).
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, header, rows):
        import csv

        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def test_row_numbers_are_zero_based_and_follow_file_order(self):
        header = ["player_id", "team_id", "season_id", "team_name"]
        self._write(
            "rows.csv",
            header,
            [
                ["p1", "t1", "16", "Club A"],
                ["p1", "t2", "16", "Club A"],  # tied team_name, different team_id
                ["p2", "t3", "16", "Club B"],
            ],
        )
        rows = _load_season_source(self.data_dir / "rows.csv", "player_history")
        numbers_by_team = {r["team_id"]: r["_source_row_number"] for r in rows}
        self.assertEqual(numbers_by_team, {"t1": 0, "t2": 1, "t3": 2})

    def test_row_number_reflects_the_surviving_row_not_the_first_occurrence(self):
        # A duplicate natural key keeps the LATER row's values (see the
        # docstring's "newest observation wins" convention) -- its
        # _source_row_number must be that same later row's own raw file
        # position, not the position of the earlier, discarded occurrence
        # dict.__setitem__ on an existing key would otherwise leave it stuck
        # at (dict key order is insertion order, not last-write order).
        header = ["player_id", "team_id", "season_id", "player_name"]
        self._write(
            "dup.csv",
            header,
            [
                ["p1", "t1", "16", "Old"],   # raw index 0, discarded
                ["p2", "t2", "16", "Other"],  # raw index 1
                ["p1", "t1", "16", "New"],   # raw index 2, duplicate of row 0, survives
            ],
        )
        rows = _load_season_source(self.data_dir / "dup.csv", "player_season_stats")
        self.assertEqual(len(rows), 2)
        p1_row = next(r for r in rows if r["player_id"] == "p1")
        p2_row = next(r for r in rows if r["player_id"] == "p2")
        self.assertEqual(p1_row["player_name"], "New")
        # The whole point: p1's surviving row is positioned AFTER p2's, since
        # it physically appears later in the file (raw index 2 vs 1) -- a
        # naive "enumerate after dedup" implementation would instead give p1
        # position 0 (its first, discarded occurrence's dict-insertion slot).
        self.assertEqual(p1_row["_source_row_number"], 2)
        self.assertEqual(p2_row["_source_row_number"], 1)

    def test_row_number_orders_correctly_against_a_row_between_the_two_occurrences(self):
        # The scenario that a naive post-dedup enumerate() gets wrong: a
        # third, distinct-key row sits between a duplicate key's first and
        # second occurrence. The surviving (later) row must still sort AFTER
        # that in-between row, matching real CSV-file order.
        header = ["player_id", "team_id", "season_id", "player_name"]
        self._write(
            "interleaved.csv",
            header,
            [
                ["p1", "t1", "16", "Old"],     # raw index 0, discarded
                ["p2", "t2", "16", "Other"],   # raw index 1
                ["p3", "t3", "16", "Third"],   # raw index 2
                ["p1", "t1", "16", "New"],     # raw index 3, duplicate of row 0, survives
            ],
        )
        rows = _load_season_source(self.data_dir / "interleaved.csv", "player_history")
        numbers = {r["player_id"]: r["_source_row_number"] for r in rows}
        self.assertEqual(numbers, {"p1": 3, "p2": 1, "p3": 2})
        # p1 (the surviving duplicate) must rank after p3, not before it.
        self.assertGreater(numbers["p1"], numbers["p3"])


class UpsertDatasetTeamLocationsTests(unittest.TestCase):
    """dataset_team_locations must mirror team_locations.csv verbatim, per team_id.

    Unlike _upsert_venues_and_teams()/_load_venue_sources() (which dedupe by
    field_id and can only keep one team's city for every field), this must
    never drop a team's own row just because another team shares its field --
    see the 0003 migration's docstring for the real repository example
    (field_id 48, 11 team_ids, two different cities).
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write_team_locations(self, rows):
        import csv

        header = ["team_id", "team_name", "field_id", "field_name", "address", "city", "district", "lat", "lon", "geocode_query", "precision"]
        with (self.data_dir / "team_locations.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)

    def test_two_teams_sharing_a_field_id_keep_their_own_distinct_city(self):
        # Reproduces field_id 48 (this repository's own data): most teams
        # geocode to the field's own locality, but one team_id's own row
        # resolves to a different, real city -- both must survive.
        self._write_team_locations(
            [
                {"team_id": "2772", "team_name": "Club Field", "field_id": "48", "field_name": "Shared Field", "address": "Shared Field", "city": "Field City", "district": "North", "lat": "32.92", "lon": "35.25", "geocode_query": "Shared Field", "precision": "fallback"},
                {"team_id": "6480", "team_name": "Club Own", "field_id": "48", "field_name": "Shared Field", "address": "Shared Field", "city": "Own City", "district": "North", "lat": "32.83", "lon": "35.50", "geocode_query": "Own City", "precision": "locality"},
            ]
        )
        cursor = _FakeCursor()
        count = _upsert_dataset_team_locations(cursor, dataset_id=1, data_dir=self.data_dir)
        self.assertEqual(count, 2)
        inserted = {
            params[1]: params  # team_id -> full row
            for sql, params in cursor.calls
            if sql.startswith("INSERT INTO dataset_team_locations")
        }
        self.assertEqual(set(inserted), {"2772", "6480"})
        # (dataset_id, team_id, field_id, field_name, city, address, lat, lon, precision)
        self.assertEqual(inserted["2772"][4], "Field City")
        self.assertEqual(inserted["6480"][4], "Own City")
        self.assertEqual(inserted["6480"][6], 32.83)

    def test_missing_file_inserts_nothing(self):
        cursor = _FakeCursor()
        count = _upsert_dataset_team_locations(cursor, dataset_id=1, data_dir=self.data_dir)
        self.assertEqual(count, 0)
        self.assertEqual(
            [c for c in cursor.calls if c[0].startswith("INSERT INTO dataset_team_locations")],
            [],
        )


if __name__ == "__main__":
    unittest.main()
