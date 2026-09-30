"""Focused orchestration tests for the safe refresh command."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ifa_scraper.validation import ValidationResult
from scripts.import_canonical_dataset import REQUIRED_SOURCE_FILENAMES
from scripts.refresh_dataset import RefreshFailed, refresh_dataset


class RefreshDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        for filename in REQUIRED_SOURCE_FILENAMES:
            (self.data_dir / filename).write_text("header\nrow\n", encoding="utf-8")
        self.row_counts = {
            "raw_season_rows": 10,
            "raw_history_rows": 20,
            "player_team_seasons_rows": 25,
            "player_team_season_observations_rows": 30,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _workflow_patches(self):
        return (
            patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6),
            patch("scripts.refresh_dataset.run_normal_scrape"),
            patch(
                "scripts.refresh_dataset.import_dataset",
                return_value=(7, ValidationResult(ok=True)),
            ),
            patch("scripts.refresh_dataset.run_parity_check", return_value=([], 1002)),
            patch("scripts.refresh_dataset.dataset_row_counts", return_value=self.row_counts),
            patch("scripts.refresh_dataset.publish", return_value=(7, 6)),
        )

    def test_happy_path_scrapes_imports_checks_candidate_parity_then_publishes(self):
        live, scrape, imported, parity, counts, published = self._workflow_patches()
        with live as live, scrape as scrape, imported as imported, parity as parity, counts as counts, published as published:
            summary = refresh_dataset(data_dir=self.data_dir, scraper_git_sha="abc123")

        scrape.assert_called_once_with(self.data_dir)
        imported.assert_called_once_with(
            database_url=None, data_dir=self.data_dir, scraper_git_sha="abc123"
        )
        parity.assert_called_once_with(
            data_dir=self.data_dir,
            database_url=None,
            sample_size=1000,
            dataset_id=7,
        )
        published.assert_called_once_with(7, database_url=None)
        self.assertEqual(summary.previous_dataset_id, 6)
        self.assertEqual(summary.dataset_id, 7)
        self.assertEqual(summary.parity_sample_size, 1002)
        self.assertTrue(summary.published)

    def test_scrape_failure_stops_before_import_or_publish(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape", side_effect=RuntimeError("IFA unavailable")
        ), patch("scripts.refresh_dataset.import_dataset") as imported, patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaisesRegex(RefreshFailed, "IFA unavailable") as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "scrape")
        imported.assert_not_called()
        published.assert_not_called()

    def test_scrape_keyboard_interrupt_stops_before_import_or_publish(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape", side_effect=KeyboardInterrupt()
        ), patch("scripts.refresh_dataset.import_dataset") as imported, patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(KeyboardInterrupt):
                refresh_dataset(data_dir=self.data_dir)

        imported.assert_not_called()
        published.assert_not_called()

    def test_missing_or_empty_required_artifact_stops_before_import_or_publish(self):
        (self.data_dir / "team_fields.csv").write_text("", encoding="utf-8")
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch("scripts.refresh_dataset.import_dataset") as imported, patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "source artifact check")
        imported.assert_not_called()
        published.assert_not_called()

    def test_import_failure_does_not_publish(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch("scripts.refresh_dataset.import_dataset", side_effect=RuntimeError("DB write failed")), patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "import")
        published.assert_not_called()

    def test_validation_failure_does_not_publish(self):
        failed = ValidationResult(ok=False, errors=["missing observations"])
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch("scripts.refresh_dataset.import_dataset", return_value=(7, failed)), patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "validation")
        self.assertEqual(caught.exception.candidate_dataset_id, 7)
        published.assert_not_called()

    def test_parity_mismatch_does_not_publish(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch(
            "scripts.refresh_dataset.import_dataset", return_value=(7, ValidationResult(ok=True))
        ), patch("scripts.refresh_dataset.run_parity_check", return_value=(["player 1 differs"], 1002)), patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "parity")
        published.assert_not_called()

    def test_parity_exception_does_not_publish(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch(
            "scripts.refresh_dataset.import_dataset", return_value=(7, ValidationResult(ok=True))
        ), patch("scripts.refresh_dataset.run_parity_check", side_effect=RuntimeError("read failed")), patch(
            "scripts.refresh_dataset.publish"
        ) as published:
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "parity")
        published.assert_not_called()

    def test_publish_failure_leaves_the_built_validated_candidate_for_resume(self):
        with patch("scripts.refresh_dataset.current_live_dataset_id", return_value=6), patch(
            "scripts.refresh_dataset.run_normal_scrape"
        ), patch(
            "scripts.refresh_dataset.import_dataset", return_value=(7, ValidationResult(ok=True))
        ), patch("scripts.refresh_dataset.run_parity_check", return_value=([], 1002)), patch(
            "scripts.refresh_dataset.dataset_row_counts", return_value=self.row_counts
        ), patch("scripts.refresh_dataset.publish", side_effect=RuntimeError("publish unavailable")):
            with self.assertRaises(RefreshFailed) as caught:
                refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(caught.exception.stage, "publish")
        self.assertEqual(caught.exception.candidate_dataset_id, 7)

    def test_no_publish_runs_all_gates_without_calling_publish(self):
        live, scrape, imported, parity, counts, published = self._workflow_patches()
        with live as live, scrape as scrape, imported as imported, parity as parity, counts as counts, published as published:
            summary = refresh_dataset(
                data_dir=self.data_dir, scraper_git_sha="abc123", publish_candidate=False
            )

        published.assert_not_called()
        self.assertFalse(summary.published)

    def test_skip_scrape_uses_existing_artifacts(self):
        live, scrape, imported, parity, counts, published = self._workflow_patches()
        with live as live, scrape as scrape, imported as imported, parity as parity, counts as counts, published as published:
            refresh_dataset(data_dir=self.data_dir, scraper_git_sha="abc123", skip_scrape=True)

        scrape.assert_not_called()

    def test_current_git_sha_is_passed_to_import_when_not_explicit(self):
        live, scrape, imported, parity, counts, published = self._workflow_patches()
        with live as live, scrape as scrape, imported as imported, parity as parity, counts as counts, published as published, patch(
            "scripts.refresh_dataset.current_git_sha", return_value="head-sha"
        ):
            refresh_dataset(data_dir=self.data_dir)

        self.assertEqual(imported.call_args.kwargs["scraper_git_sha"], "head-sha")

    def test_candidate_parity_uses_explicit_dataset_without_changing_the_live_selection(self):
        live, scrape, imported, parity, counts, published = self._workflow_patches()
        with live as live, scrape as scrape, imported as imported, parity as parity, counts as counts, published as published:
            refresh_dataset(data_dir=self.data_dir, scraper_git_sha="abc123", publish_candidate=False)

        self.assertEqual(live.call_count, 1)
        self.assertEqual(parity.call_args.kwargs["dataset_id"], 7)


if __name__ == "__main__":
    unittest.main()
