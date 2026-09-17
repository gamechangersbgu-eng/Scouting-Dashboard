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

from dashboard.app_core import merge_canonical_rows
from scripts.import_canonical_dataset import (
    _bool_from_csv,
    _insert_facts,
    _int_or_none,
    _load_season_source,
    _load_venue_sources,
    _text_or_none,
    _upsert_leagues_and_memberships,
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

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)


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
        self.assertEqual(insert_calls[0][1]["player_name"], "Real Name")


if __name__ == "__main__":
    unittest.main()
