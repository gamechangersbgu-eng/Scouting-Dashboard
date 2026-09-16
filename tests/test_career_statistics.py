import csv
import tempfile
import unittest
from pathlib import Path

from dashboard.app_core import ScoutingData, create_app


STAT_FIELDS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]


def stats(**values):
    return {field: values.get(field, 0) for field in STAT_FIELDS}


def record(player_id, season_id, team_id, *, available=True, source=None, **values):
    row = {
        "player_id": player_id,
        "player_name": f"Player {player_id}",
        "season_id": season_id,
        "season": f"20{season_id}/xx",
        "team_id": team_id,
        "team_name": f"Team {team_id}",
        "league_id": "101",
        "league_name": "Youth league",
        "age_group": "Youth",
        "stats_available": str(available).lower(),
        "stats_source": source or ("team_player_statistics" if available else "registration_only"),
        "stats_completeness": "full" if available else "unavailable",
    }
    row.update(stats(**values) if available else {field: "" for field in STAT_FIELDS})
    return row


class CareerStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self._write_fixture_data()
        self.data = ScoutingData(self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def _csv(self, name, rows):
        columns = list(rows[0]) if rows else []
        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def _write_fixture_data(self):
        self._csv("players_youth.csv", [
            {"player_id": "p1", "player_name": "Player p1", "birth_year": "2011", "image_url": "", "current_team": "Team current", "age_groups": "Youth", "leagues": "Youth league", "plays_above_age": "No", "age_groups_above": "0", "above_age_history": "", "goals_total": "0", "games_total": "0", "minutes_total": "0"},
            {"player_id": "p2", "player_name": "Player p2", "birth_year": "2011", "image_url": "", "current_team": "", "age_groups": "Youth", "leagues": "", "plays_above_age": "No", "age_groups_above": "0", "above_age_history": "", "goals_total": "0", "games_total": "0", "minutes_total": "0"},
            {"player_id": "p3", "player_name": "Player p3", "birth_year": "2011", "image_url": "", "current_team": "", "age_groups": "Youth", "leagues": "", "plays_above_age": "No", "age_groups_above": "0", "above_age_history": "", "goals_total": "0", "games_total": "0", "minutes_total": "0"},
        ])
        # The recent duplicate is more complete than the stale historical copy and
        # must therefore be selected once, not added to it.
        self._csv("player_season_stats.csv", [record("p1", 24, "current", games=3, goals=1, minutes=180, starts=2)])
        self._csv("player_history.csv", [
            record("p1", 24, "current", games=99, goals=99, minutes=999),
            record("p1", 22, "old", games=18, goals=4, minutes=1260, starts=18, yellow_cards_league_cup=2),
            record("p1", 22, "second", games=0, goals=0, minutes=0),
            record("p1", 23, "unknown", available=False),
            record("p2", 20, "city-a", games=1, minutes=60),
            record("p2", 20, "city-b", games=1, minutes=60),
            record("p3", 20, "unlocated", games=1, minutes=60),
        ])
        self._csv("team_locations.csv", [
            {"team_id": "old", "city": "Beer Sheva", "lat": "31.25", "lon": "34.79", "field_name": "Old", "address": "", "precision": "street"},
            {"team_id": "second", "city": "Beer Sheva", "lat": "31.25", "lon": "34.79", "field_name": "Second", "address": "", "precision": "street"},
            {"team_id": "current", "city": "Kiryat Gat", "lat": "31.61", "lon": "34.76", "field_name": "Current", "address": "", "precision": "street"},
            {"team_id": "city-a", "city": "A", "lat": "31", "lon": "34", "field_name": "A", "address": "", "precision": "street"},
            {"team_id": "city-b", "city": "B", "lat": "32", "lon": "35", "field_name": "B", "address": "", "precision": "street"},
        ])

    def test_detailed_history_zero_and_unknown_are_distinct(self):
        seasons = {row["team_id"]: row for row in self.data.player("p1")["seasons"]}
        self.assertEqual(seasons["old"]["games"], 18)
        self.assertEqual(seasons["old"]["goals"], 4)
        self.assertTrue(seasons["old"]["stats_available"])
        self.assertEqual(seasons["second"]["goals"], 0)
        self.assertTrue(seasons["second"]["registered_no_games"])
        self.assertIsNone(seasons["unknown"]["goals"])
        self.assertFalse(seasons["unknown"]["stats_available"])

    def test_duplicate_recent_history_row_is_not_double_counted(self):
        player = self.data.player("p1")
        self.assertEqual(player["totals"]["games_total"], 21)
        self.assertEqual(player["totals"]["goals_total"], 5)
        self.assertEqual(len([row for row in player["seasons"] if row["team_id"] == "current"]), 1)

    def test_two_teams_in_one_season_are_preserved(self):
        player = self.data.player("p1")
        same_season = [row for row in player["seasons"] if row["season_id"] == 22]
        self.assertEqual({row["team_id"] for row in same_season}, {"old", "second"})

    def test_likely_origin_cases(self):
        known = self.data.player("p1")
        self.assertEqual(known["likely_origin_city"], "Beer Sheva")
        self.assertIn("clubs", known["likely_origin_basis"])
        ambiguous = self.data.player("p2")
        self.assertIsNone(ambiguous["likely_origin_city"])
        self.assertEqual(ambiguous["likely_origin_confidence"], "ambiguous")
        unknown = self.data.player("p3")
        self.assertIsNone(unknown["likely_origin_city"])
        self.assertIsNone(unknown["likely_origin_confidence"])

    def test_player_detail_api_serialises_career_statistics(self):
        response = create_app(self.data_dir).test_client().get("/api/player/p1")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["totals"]["minutes_total"], 1440)
        self.assertEqual(
            next(row for row in payload["seasons"] if row["team_id"] == "unknown")["stats_source"],
            "registration_only",
        )


if __name__ == "__main__":
    unittest.main()
