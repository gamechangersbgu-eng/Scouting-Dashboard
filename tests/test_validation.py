"""Unit tests for the observation-layer validation rules added by the 0002 migration.

Fakes the cursor rather than talking to a real database, following the same
convention as the ``_FakeCursor`` in ``tests/test_import_canonical_dataset.py``
(the parts of the pipeline that actually talk to Postgres are verified
manually against a real instance -- see that file's own docstring). Only the
three rules ``ifa_scraper.validation`` gained for
``player_team_season_observations`` are covered here; the pre-existing rules
already shipped with 0001 and are unchanged by this migration.
"""

import unittest

from ifa_scraper.validation import (
    ValidationResult,
    _check_every_canonical_row_has_an_observation,
    _check_observation_referential_integrity,
    _check_observation_row_count_drop,
)


class _ScriptedCursor:
    """Returns queued responses for whichever SQL substring matches, in order.

    Some validation queries share identical SQL text across two calls (e.g.
    ``_check_observation_row_count_drop`` runs the same GROUP BY query once
    for the candidate dataset and once for the previous one) -- a plain
    marker->response map can't tell those apart, so each marker holds a
    *queue* of responses consumed first-in-first-out.
    """

    def __init__(self, responses_by_marker):
        self.responses_by_marker = {marker: list(responses) for marker, responses in responses_by_marker.items()}
        self._pending = None

    def execute(self, sql, params=None):
        for marker, queue in self.responses_by_marker.items():
            if marker in sql:
                if not queue:
                    raise AssertionError(f"no more scripted responses for marker {marker!r}")
                self._pending = queue.pop(0)
                return
        raise AssertionError(f"no scripted response registered for query: {sql[:80]}")

    def fetchone(self):
        return self._pending

    def fetchall(self):
        return self._pending


class ObservationReferentialIntegrityTests(unittest.TestCase):
    def test_clean_dataset_reports_no_errors(self):
        cursor = _ScriptedCursor(
            {
                "FROM players p WHERE p.player_id = o.player_id": [(0,)],
                "FROM teams t WHERE t.team_id = o.team_id": [(0,)],
                "FROM seasons s WHERE s.season_id = o.season_id": [(0,)],
            }
        )
        result = ValidationResult(ok=True)
        _check_observation_referential_integrity(cursor, dataset_id=1, result=result)
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])

    def test_orphaned_rows_are_reported_per_dimension(self):
        cursor = _ScriptedCursor(
            {
                "FROM players p WHERE p.player_id = o.player_id": [(3,)],
                "FROM teams t WHERE t.team_id = o.team_id": [(0,)],
                "FROM seasons s WHERE s.season_id = o.season_id": [(2,)],
            }
        )
        result = ValidationResult(ok=True)
        _check_observation_referential_integrity(cursor, dataset_id=1, result=result)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(any("unknown player_id" in e for e in result.errors))
        self.assertTrue(any("unknown season_id" in e for e in result.errors))


class CanonicalRowsHaveObservationsTests(unittest.TestCase):
    def test_no_orphaned_canonical_rows_passes(self):
        cursor = _ScriptedCursor({"o.dataset_id = pts.dataset_id": [(0,)]})
        result = ValidationResult(ok=True)
        _check_every_canonical_row_has_an_observation(cursor, dataset_id=1, result=result)
        self.assertTrue(result.ok)

    def test_orphaned_canonical_row_fails_validation(self):
        # This is exactly the bug class the 0002 migration exists to catch: a
        # canonical player_team_seasons row with no backing observation means
        # the two writes have drifted apart.
        cursor = _ScriptedCursor({"o.dataset_id = pts.dataset_id": [(7,)]})
        result = ValidationResult(ok=True)
        _check_every_canonical_row_has_an_observation(cursor, dataset_id=1, result=result)
        self.assertFalse(result.ok)
        self.assertIn("drifted apart", result.errors[0])


class ObservationRowCountDropTests(unittest.TestCase):
    def test_no_previous_dataset_skips_the_check(self):
        cursor = _ScriptedCursor({})
        result = ValidationResult(ok=True)
        _check_observation_row_count_drop(cursor, dataset_id=2, previous_dataset_id=None, result=result)
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])

    def test_retained_counts_pass(self):
        # First call = candidate dataset, second = previous dataset (see
        # import_dataset()'s call order in scripts/import_canonical_dataset.py
        # -- validate_dataset always evaluates the candidate first).
        cursor = _ScriptedCursor(
            {
                "GROUP BY source_file": [
                    [("player_season_stats", 49000), ("player_history", 194000)],
                    [("player_season_stats", 49593), ("player_history", 194518)],
                ]
            }
        )
        result = ValidationResult(ok=True)
        _check_observation_row_count_drop(cursor, dataset_id=2, previous_dataset_id=1, result=result)
        self.assertTrue(result.ok)

    def test_one_source_collapsing_independently_fails_even_if_the_other_is_fine(self):
        # A bug that only breaks writing the recent source's observations
        # (e.g. player_season_stats.csv failing to parse) must be caught even
        # though player_history's own count is untouched -- this is exactly
        # why the check is per source_file rather than only on the combined
        # player_team_seasons total (see the function's docstring).
        cursor = _ScriptedCursor(
            {
                "GROUP BY source_file": [
                    [("player_season_stats", 10), ("player_history", 194518)],  # candidate
                    [("player_season_stats", 49593), ("player_history", 194518)],  # previous
                ]
            }
        )
        result = ValidationResult(ok=True)
        _check_observation_row_count_drop(cursor, dataset_id=2, previous_dataset_id=1, result=result)
        self.assertFalse(result.ok)
        self.assertIn("player_season_stats", result.errors[0])


if __name__ == "__main__":
    unittest.main()
