"""Player-detail fetch scope, checkpoint preservation, and TTL tests."""

import csv
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ifa_scraper import run


class _PlayerPageClient:
    def __init__(self):
        self.calls = []

    def player_page(self, player_id):
        self.calls.append(player_id)
        return player_id


def _detail(player_id):
    return {
        "birth_year": 2010,
        "birth_month": 3,
        "image_url": f"https://example.test/{player_id}.jpg",
    }


class PlayerDetailRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "player_details.csv"
        self.now = datetime(2026, 9, 19, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def _write_checkpoint(self, columns, rows):
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def test_only_current_and_previous_season_players_are_eligible_for_detail_fetches(self):
        rows = [
            {"player_id": "current", "season_id": 28},
            {"player_id": "previous", "season_id": 27},
            {"player_id": "inactive", "season_id": 26},
            {"player_id": "current", "season_id": 28},
        ]
        self.assertEqual(
            run.detail_refresh_player_ids(rows),
            ["current", "previous"],
        )

    def test_legacy_checkpoint_migration_retains_and_reuses_inactive_cached_details(self):
        self._write_checkpoint(
            run.DETAIL_COLUMNS[:4],
            [
                {"player_id": "active", "birth_year": "2010", "birth_month": "3", "image_url": "active.jpg"},
                {"player_id": "inactive", "birth_year": "2006", "birth_month": "8", "image_url": "inactive.jpg"},
            ],
        )
        client = _PlayerPageClient()
        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail):
            details = run.phase4_player_details(
                client,
                ["active", "new-active"],
                self.path,
                now=self.now,
            )

        # Legacy rows are known successful checkpoint records.  Migration gives
        # them one durable timestamp from the CSV mtime, so only the truly new
        # active player makes a remote request.
        self.assertEqual(client.calls, ["new-active"])
        self.assertIn("inactive", details)
        self.assertEqual(details["inactive"]["image_url"], "inactive.jpg")
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            rows = {row["player_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(set(rows), {"active", "inactive", "new-active"})
        self.assertEqual(rows["inactive"]["image_url"], "inactive.jpg")
        self.assertIn("details_fetched_at", rows["inactive"])
        self.assertTrue(rows["inactive"]["details_fetched_at"])

    def test_ttl_fetches_only_missing_or_stale_active_players(self):
        self._write_checkpoint(
            run.DETAIL_COLUMNS,
            [
                {
                    "player_id": "fresh", "birth_year": "2010", "birth_month": "3", "image_url": "fresh.jpg",
                    "details_fetched_at": (self.now - timedelta(days=1)).isoformat(),
                },
                {
                    "player_id": "stale", "birth_year": "2010", "birth_month": "3", "image_url": "stale.jpg",
                    "details_fetched_at": (self.now - timedelta(days=31)).isoformat(),
                },
                {
                    "player_id": "inactive", "birth_year": "2006", "birth_month": "8", "image_url": "inactive.jpg",
                    "details_fetched_at": (self.now - timedelta(days=365)).isoformat(),
                },
            ],
        )
        client = _PlayerPageClient()
        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail):
            run.phase4_player_details(
                client,
                ["fresh", "stale", "missing"],
                self.path,
                ttl_days=30,
                now=self.now,
            )

        self.assertEqual(client.calls, ["stale", "missing"])

    def test_restart_skips_checkpointed_players_and_fetches_only_unfinished_due_players(self):
        client = _PlayerPageClient()
        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail):
            run.phase4_player_details(
                client,
                ["completed"],
                self.path,
                now=self.now,
                checkpoint_batch_size=1,
            )
            run.phase4_player_details(
                client,
                ["unfinished", "completed"],
                self.path,
                now=self.now,
                checkpoint_batch_size=1,
            )

        self.assertEqual(client.calls, ["completed", "unfinished"])

    def test_checkpoint_is_atomic_and_keeps_one_canonical_row_per_player(self):
        client = _PlayerPageClient()
        real_replace = os.replace
        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail), patch(
            "ifa_scraper.run.os.replace", side_effect=real_replace
        ) as replace:
            run.phase4_player_details(
                client,
                ["same", "same", "other"],
                self.path,
                now=self.now,
                checkpoint_batch_size=1,
            )

        self.assertGreaterEqual(replace.call_count, 2)
        self.assertTrue(all(str(call.args[0]).endswith(".tmp") for call in replace.call_args_list))
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["player_id"] for row in rows], ["other", "same"])

    def test_keyboard_interrupt_flushes_completed_results_and_keeps_csv_valid(self):
        checkpointed_fast = __import__("threading").Event()

        class _InterruptingClient(_PlayerPageClient):
            def player_page(self, player_id):
                self.calls.append(player_id)
                if player_id == "interrupt":
                    checkpointed_fast.wait(timeout=2)
                    raise KeyboardInterrupt()
                return player_id

        client = _InterruptingClient()
        original_checkpoint = run._rewrite_detail_checkpoint

        def checkpoint(path, details):
            original_checkpoint(path, details)
            if "fast" in details:
                checkpointed_fast.set()

        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail), patch(
            "ifa_scraper.run._rewrite_detail_checkpoint", side_effect=checkpoint
        ):
            with self.assertRaises(KeyboardInterrupt):
                run.phase4_player_details(
                    client,
                    ["fast", "interrupt"],
                    self.path,
                    now=self.now,
                    checkpoint_batch_size=1,
                )

        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["player_id"] for row in rows], ["fast"])
        self.assertEqual(set(rows[0]), set(run.DETAIL_COLUMNS))

    def test_as_completed_checkpoints_a_fast_later_player_before_a_slow_earlier_player(self):
        slow_can_finish = __import__("threading").Event()
        checkpoint_snapshots = []

        class _OutOfOrderClient(_PlayerPageClient):
            def player_page(self, player_id):
                self.calls.append(player_id)
                if player_id == "slow":
                    slow_can_finish.wait(timeout=2)
                return player_id

        client = _OutOfOrderClient()
        original_checkpoint = run._rewrite_detail_checkpoint

        def checkpoint(path, details):
            checkpoint_snapshots.append(set(details))
            if "fast" in details:
                slow_can_finish.set()
            original_checkpoint(path, details)

        with patch("ifa_scraper.run.parse.parse_player_details", side_effect=_detail), patch(
            "ifa_scraper.run._rewrite_detail_checkpoint", side_effect=checkpoint
        ):
            run.phase4_player_details(
                client,
                ["slow", "fast"],
                self.path,
                now=self.now,
                checkpoint_batch_size=1,
            )

        self.assertEqual(checkpoint_snapshots[0], {"fast"})

    def test_main_passes_only_active_player_ids_to_the_existing_detail_phase(self):
        season_rows = [
            {"player_id": "current", "season_id": 28, "team_name": "A", "age_group": "Youth", "league_name": "League"},
            {"player_id": "previous", "season_id": 27, "team_name": "B", "age_group": "Youth", "league_name": "League"},
            {"player_id": "inactive", "season_id": 26, "team_name": "C", "age_group": "Youth", "league_name": "League"},
        ]

        class _Client:
            stats = {}

        with patch("ifa_scraper.run.IFAClient", return_value=_Client()), patch(
            "ifa_scraper.run.phase1_collect_teams", return_value=({}, [])
        ), patch("ifa_scraper.run.phase2_collect_squads", return_value=season_rows), patch(
            "ifa_scraper.run.phase4_player_details", return_value={}
        ) as details_phase, patch("ifa_scraper.run.aggregate_players", return_value=[]), patch(
            "ifa_scraper.run.write_csv"
        ), patch("ifa_scraper.run.single_run_lock", return_value=nullcontext()):
            run.main(
                argv=["--leagues-from-config", "--skip-goal-split"],
                data_dir=self.path.parent,
            )

        self.assertEqual(details_phase.call_args.args[1], ["current", "previous"])


if __name__ == "__main__":
    unittest.main()
