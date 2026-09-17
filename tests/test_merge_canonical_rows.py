"""dashboard.app_core.merge_canonical_rows(): the one merge algorithm shared by
BaseScoutingData.player() (CSV, read-time) and scripts.import_canonical_dataset
(Postgres, import-time). Tested directly so the two call sites can never drift
without a test catching it.
"""

import unittest

from dashboard.app_core import merge_canonical_rows


def _row(player_id="p1", team_id="t1", season_id=26, **extra):
    row = {
        "player_id": player_id,
        "team_id": team_id,
        "season_id": season_id,
        "player_name": "Player",
        "team_name": "Team",
        "league_name": "League",
        "age_group": "Youth",
        "stats_available": True,
        "stats_completeness": "full",
    }
    row.update(extra)
    return row


class MergeCanonicalRowsTests(unittest.TestCase):
    def test_single_row_passes_through_unchanged(self):
        merged = merge_canonical_rows([_row()])
        self.assertEqual(len(merged), 1)

    def test_two_sources_same_key_collapse_to_one_row(self):
        rows = [
            _row(player_name="History Row", stats_completeness="partial"),
            _row(player_name="Recent Row", stats_completeness="full"),
        ]
        merged = merge_canonical_rows(rows)
        self.assertEqual(len(merged), 1)
        (row,) = merged.values()
        self.assertEqual(row["player_name"], "Recent Row")  # higher stats quality wins

    def test_masked_name_never_wins_over_known_even_with_better_stats(self):
        rows = [
            _row(player_name="Yosef Cohen", stats_completeness="partial"),
            _row(player_name="*******", stats_completeness="full"),
        ]
        merged = merge_canonical_rows(rows)
        (row,) = merged.values()
        self.assertEqual(row["stats_completeness"], "full")  # stats still come from the winner
        self.assertEqual(row["player_name"], "Yosef Cohen")  # but the known name is kept

    def test_later_row_wins_a_true_tie(self):
        rows = [
            _row(team_name="First"),
            _row(team_name="Second"),
        ]
        merged = merge_canonical_rows(rows)
        (row,) = merged.values()
        self.assertEqual(row["team_name"], "Second")

    def test_missing_descriptive_fields_backfill_from_the_loser(self):
        rows = [
            # Loser on stats quality, but carries the descriptive fields.
            _row(league_name="League", age_group="Youth", stats_completeness="partial"),
            # Winner on stats quality, but missing the descriptive fields.
            _row(league_name="", age_group="", stats_completeness="full"),
        ]
        merged = merge_canonical_rows(rows)
        (row,) = merged.values()
        self.assertEqual(row["league_name"], "League")
        self.assertEqual(row["age_group"], "Youth")

    def test_different_keys_never_merge(self):
        rows = [_row(team_id="t1"), _row(team_id="t2")]
        merged = merge_canonical_rows(rows)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
