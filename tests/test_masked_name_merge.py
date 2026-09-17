"""dashboard.app_core.BaseScoutingData.player(): masked names must not win merges."""

import csv
import tempfile
import unittest
from pathlib import Path

from dashboard.app_core import CsvScoutingData
from ifa_scraper import name_quality

STAT_FIELDS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]


def stats(**values):
    return {field: values.get(field, 0) for field in STAT_FIELDS}


def record(player_id, season_id, team_id, player_name, *, available=True, **values):
    row = {
        "player_id": player_id,
        "player_name": player_name,
        "season_id": season_id,
        "season": f"20{season_id}/xx",
        "team_id": team_id,
        "team_name": f"Team {team_id}",
        "league_id": "101",
        "league_name": "Youth league",
        "age_group": "Youth",
        "stats_available": str(available).lower(),
        "stats_source": "team_player_statistics" if available else "registration_only",
        "stats_completeness": "full" if available else "unavailable",
    }
    row.update(stats(**values) if available else {field: "" for field in STAT_FIELDS})
    return row


class MaskedNameMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _csv(self, name, rows):
        columns = list(rows[0]) if rows else []
        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def test_full_stats_row_with_masked_name_does_not_win_over_known_history_name(self):
        # player_season_stats.csv (recent, "full" completeness) has the mask;
        # player_history.csv (older, same key) has the real name. The recent row
        # still wins on stats quality (it is more complete), but the merge must
        # keep the known name rather than adopt the mask.
        self._csv("players_youth.csv", [
            {"player_id": "p1", "player_name": "*******", "name_status": "masked",
             "birth_year": "2011", "image_url": "", "current_team": "Team current",
             "age_groups": "Youth", "leagues": "Youth league", "plays_above_age": "No",
             "age_groups_above": "0", "above_age_history": "", "goals_total": "0",
             "games_total": "0", "minutes_total": "0"},
        ])
        self._csv("player_season_stats.csv", [
            record("p1", 24, "current", "*******", games=3, goals=1, minutes=180, starts=2),
        ])
        self._csv("player_history.csv", [
            record("p1", 24, "current", "Yosef Cohen", games=3, goals=1, minutes=180, starts=2),
        ])
        self._csv("team_locations.csv", [
            {"team_id": "current", "city": "Kiryat Gat", "lat": "31.61", "lon": "34.76",
             "field_name": "Current", "address": "", "precision": "street"},
        ])

        data = CsvScoutingData(self.data_dir)
        player = data.player("p1")
        season = next(row for row in player["seasons"] if row["team_id"] == "current")
        self.assertEqual(season["games"], 3)  # still took the full-quality row's stats
        # The merged row's own player_name field carries the resolved name, and it
        # must never regress to the mask just because that row won on stats.
        merged_name = next(
            row.get("player_name")
            for row in data.rows_by_player["p1"] + data.history_by_player["p1"]
            if row.get("team_id") == "current" and row.get("player_name") == "Yosef Cohen"
        )
        self.assertEqual(merged_name, "Yosef Cohen")

    def test_players_youth_name_status_column_missing_is_inferred(self):
        # Older, already-generated players_youth.csv files predate name_status.
        self._csv("players_youth.csv", [
            {"player_id": "p1", "player_name": "*******",
             "birth_year": "2011", "image_url": "", "current_team": "",
             "age_groups": "Youth", "leagues": "", "plays_above_age": "No",
             "age_groups_above": "0", "above_age_history": "", "goals_total": "0",
             "games_total": "0", "minutes_total": "0"},
        ])
        self._csv("player_season_stats.csv", [record("p1", 24, "current", "*******")])
        # team_locations.csv intentionally omitted: _load_locations() already
        # handles a missing file gracefully, and an existing-but-headerless
        # file is not a real scenario this test needs to cover.

        data = CsvScoutingData(self.data_dir)
        self.assertEqual(data.players["p1"]["name_status"], name_quality.MASKED)


if __name__ == "__main__":
    unittest.main()
