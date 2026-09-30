"""Tests for validating an existing dataset without rebuilding or publishing."""

import unittest
from unittest.mock import patch

from ifa_scraper.validation import ValidationResult
from scripts.dataset_validation import validate_and_record_dataset
from scripts.validate_dataset import validate_existing_dataset


class _Cursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        self.connection.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.connection.responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Connection:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class ValidateAndRecordDatasetTests(unittest.TestCase):
    def test_existing_dataset_uses_local_timeout_and_records_success(self):
        connection = _Connection(responses=[("building",), (5,)])
        expected = ValidationResult(ok=True)

        with patch(
            "scripts.dataset_validation.validation.validate_dataset",
            return_value=expected,
        ) as validate:
            result = validate_and_record_dataset(connection, dataset_id=6)

        self.assertIs(result, expected)
        self.assertEqual(connection.calls[0][0], "SET LOCAL statement_timeout = '30min'")
        self.assertEqual(
            connection.calls[1],
            ("SELECT status FROM dataset_versions WHERE dataset_id = %s", (6,)),
        )
        self.assertEqual(
            connection.calls[2][0],
            "SELECT dataset_id FROM current_dataset WHERE id",
        )
        validate.assert_called_once_with(connection, 6, 5)
        self.assertEqual(
            connection.calls[3],
            (
                "UPDATE dataset_versions SET status = %s, finished_at = now(), notes = %s WHERE dataset_id = %s",
                ("validated", None, 6),
            ),
        )

    def test_nonexistent_dataset_is_refused_without_validation_or_status_update(self):
        connection = _Connection(responses=[None])

        with patch("scripts.dataset_validation.validation.validate_dataset") as validate:
            with self.assertRaisesRegex(RuntimeError, "dataset_id=6 does not exist"):
                validate_and_record_dataset(connection, dataset_id=6)

        validate.assert_not_called()
        self.assertEqual(
            [sql for sql, _params in connection.calls],
            [
                "SET LOCAL statement_timeout = '30min'",
                "SELECT status FROM dataset_versions WHERE dataset_id = %s",
            ],
        )


class ValidateExistingDatasetTests(unittest.TestCase):
    def test_analyze_runs_and_commits_before_the_separate_validation_transaction(self):
        maintenance_connection = _Connection()
        validation_connection = _Connection(responses=[("failed",), (5,)])
        expected = ValidationResult(ok=False, errors=["bad data"])

        with patch(
            "scripts.validate_dataset.db.connect",
            side_effect=[maintenance_connection, validation_connection],
        ), patch(
            "scripts.dataset_validation.validation.validate_dataset",
            return_value=expected,
        ):
            result = validate_existing_dataset(6, analyze=True)

        self.assertIs(result, expected)
        self.assertEqual(maintenance_connection.commits, 1)
        self.assertEqual(
            [sql for sql, _params in maintenance_connection.calls],
            [
                "SET LOCAL statement_timeout = '30min'",
                "ANALYZE player_team_season_observations",
                "ANALYZE player_team_seasons",
            ],
        )
        self.assertEqual(validation_connection.commits, 1)
        self.assertEqual(
            validation_connection.calls[-1][1],
            ("failed", "bad data", 6),
        )


if __name__ == "__main__":
    unittest.main()
