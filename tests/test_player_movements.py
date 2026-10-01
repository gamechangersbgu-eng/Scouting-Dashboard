"""Focused Hapoel Be'er Sheva movement-report semantics.

The IFA source describes season memberships, not dated transfers.  These
fixtures deliberately model only that grain, including same-season ambiguity.
"""

import csv
import math
import tempfile
import unittest
from pathlib import Path

from dashboard.app_core import CsvScoutingData, create_app, is_hapoel_beer_sheva_team


STAT_FIELDS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]


def membership(player_id, season_id, team_id, team_name, *, season=None):
    """Create a complete canonical-season CSV row for an observed membership."""
    return {
        "player_id": player_id,
        "player_name": f"Player {player_id}",
        "season_id": str(season_id),
        "season": season or f"20{season_id}/xx",
        "team_id": team_id,
        "team_name": team_name,
        "league_id": "1",
        "league_name": "Youth",
        "age_group": "Youth",
        "stats_available": "true",
        "stats_source": "team_player_statistics",
        "stats_completeness": "full",
        **{field: "0" for field in STAT_FIELDS},
    }


class PlayerMovementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self._write_fixture()
        self.data = CsvScoutingData(self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def _write_csv(self, name, rows):
        with (self.data_dir / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def _write_fixture(self):
        hbs = ('2085', 'הפועל ב"ש')
        rows = [
            # Former players: the latest external membership, not the first
            # club after Hapoel, is the current observed club.
            membership("former-one", 20, *hbs),
            membership("former-one", 21, "haifa", "מכבי חיפה"),
            membership("former-many", 20, "1440", "הפועל באר שבע"),
            membership("former-many", 21, "haifa", "מכבי חיפה"),
            membership("former-many", 22, "tel-aviv", "מכבי תל אביב"),
            # Two different verified Hapoel youth IDs are one club scope.
            membership("former-multi-hapoel", 20, "2085", 'הפועל ב"ש'),
            membership("former-multi-hapoel", 21, "1444", "הפועל באר שבע"),
            membership("former-multi-hapoel", 23, "haifa", "מכבי חיפה"),
            membership("still-hapoel", 20, "haifa", "מכבי חיפה"),
            membership("still-hapoel", 21, "1443", "הפועל באר שבע"),
            # The published youth-team name is still Hapoel Be'er Sheva.
            membership("three-youth", 20, "haifa", "מכבי חיפה"),
            membership("three-youth", 21, "2373", 'הפ\' ב"ש השלושה'),
            # Arrival and returnee: the source is before the *current* spell.
            membership("arrival", 20, "netanya", "מכבי נתניה"),
            # The observed seasons are deliberately not consecutive: absent
            # seasons are incomplete source coverage, not a spell boundary.
            membership("arrival", 23, "1444", "הפועל באר שבע"),
            membership("returnee", 20, "tel-aviv", "מכבי תל אביב"),
            membership("returnee", 21, "2181", "הפועל באר שבע"),
            membership("returnee", 22, "netanya", "מכבי נתניה"),
            membership("returnee", 23, "6485", "הפ' באר שבע אדום"),
            membership("only-hapoel", 21, "2755", "הפועל ב\"ש אדום"),
            # This does not establish a within-season direction.
            membership("ambiguous", 21, "2938", "הפועל באר שבע 2"),
            membership("ambiguous", 21, "haifa", "מכבי חיפה"),
            # Preserve every membership in the earlier external season.
            membership("multi-source", 20, "haifa", "מכבי חיפה"),
            membership("multi-source", 20, "tel-aviv", "מכבי תל אביב"),
            membership("multi-source", 21, "7447", "הפועל ב\"ש 3"),
            # A nearby club name must not make a player a Hapoel player.
            membership("not-hapoel", 20, "1039", "מכבי באר שבע"),
            membership("not-hapoel", 21, "haifa", "מכבי חיפה"),
        ]
        # A history/recent duplicate shares the same canonical key.  The
        # report must still return one former-player result for this player.
        history = [membership("former-one", 20, *hbs)]
        self._write_csv("player_season_stats.csv", rows)
        self._write_csv("player_history.csv", history)
        players = []
        for player_id in sorted({row["player_id"] for row in rows}):
            players.append(
                {
                    "player_id": player_id,
                    "player_name": f"Player {player_id}",
                    "birth_year": "2009",
                    "image_url": "",
                    "current_team": "",
                    "age_groups": "Youth",
                    "leagues": "Youth",
                    "plays_above_age": "No",
                    "age_groups_above": "0",
                    "above_age_history": "",
                    "goals_total": "0",
                    "games_total": "0",
                    "minutes_total": "0",
                }
            )
        self._write_csv("players_youth.csv", players)

    def test_hapoel_scope_is_exact_and_covers_verified_variants(self):
        self.assertTrue(is_hapoel_beer_sheva_team({"team_id": "2085", "team_name": 'הפועל ב"ש'}))
        self.assertTrue(is_hapoel_beer_sheva_team({"team_id": "new", "team_name": "הפועל באר שבע"}))
        self.assertTrue(is_hapoel_beer_sheva_team({"team_id": "2373", "team_name": "anything"}))
        for team_name in ("הפועל באר שבע השלושה", 'הפועל ב"ש השלושה', 'הפ\' ב"ש השלושה'):
            with self.subTest(team_name=team_name):
                self.assertTrue(
                    is_hapoel_beer_sheva_team({"team_id": "new-youth", "team_name": team_name})
                )
        self.assertTrue(is_hapoel_beer_sheva_team({"team_id": "6485", "team_name": "anything"}))
        self.assertFalse(is_hapoel_beer_sheva_team({"team_id": "1039", "team_name": "מכבי באר שבע"}))
        self.assertFalse(is_hapoel_beer_sheva_team({"team_id": "7985", "team_name": "מ.ס באר שבע 2"}))

    def test_former_players_use_latest_external_clubs(self):
        report = self.data.player_movements()
        former = {entry["player_id"]: entry for entry in report["former_players"]}
        self.assertEqual([club["team_name"] for club in former["former-one"]["current_clubs"]], ["מכבי חיפה"])
        self.assertEqual([club["team_name"] for club in former["former-many"]["current_clubs"]], ["מכבי תל אביב"])
        self.assertNotIn("still-hapoel", former)
        self.assertNotIn("not-hapoel", former)
        self.assertEqual(len([entry for entry in report["former_players"] if entry["player_id"] == "former-one"]), 1)

    def test_former_hapoel_flag_uses_all_observed_memberships(self):
        # The same flag is exposed by both list/search data and player detail.
        search = {entry["player_id"]: entry for entry in self.data.search("")}
        self.assertTrue(search["former-one"]["former_hapoel_player"])
        self.assertTrue(search["former-multi-hapoel"]["former_hapoel_player"])
        self.assertFalse(search["still-hapoel"]["former_hapoel_player"])
        self.assertFalse(search["three-youth"]["former_hapoel_player"])
        self.assertFalse(search["returnee"]["former_hapoel_player"])
        self.assertFalse(search["not-hapoel"]["former_hapoel_player"])
        self.assertFalse(search["ambiguous"]["former_hapoel_player"])

        self.assertTrue(self.data.player("former-one")["former_hapoel_player"])
        self.assertFalse(self.data.player("returnee")["former_hapoel_player"])
        report = self.data.player_movements()
        former_ids = {entry["player_id"] for entry in report["former_players"]}
        current_ids = {entry["player_id"] for entry in report["current_players"]}
        self.assertIn("former-multi-hapoel", former_ids)
        self.assertIn("returnee", current_ids)
        self.assertIn("three-youth", current_ids)

    def test_initialization_and_movements_only_cache_hapoel_timelines(self):
        # Building the search index must not materialize a canonical career for
        # every player merely to determine the EX badge.
        self.assertEqual(self.data._timeline_cache, {})

        self.data.player_movements()

        # The report only needs canonical timelines for players with at least
        # one Hapoel membership; unrelated catalog players remain uncached.
        self.assertEqual(
            set(self.data._timeline_cache),
            set(self.data._hapoel_candidate_player_ids),
        )
        self.assertNotIn("not-hapoel", self.data._timeline_cache)

    def test_current_player_origin_uses_current_spell_and_preserves_multiple_clubs(self):
        report = self.data.player_movements()
        current = {entry["player_id"]: entry for entry in report["current_players"]}
        self.assertEqual([club["team_name"] for club in current["arrival"]["previous_clubs"]], ["מכבי נתניה"])
        self.assertEqual([club["team_name"] for club in current["returnee"]["previous_clubs"]], ["מכבי נתניה"])
        self.assertEqual(current["returnee"]["current_spell_start_season_id"], 23)
        self.assertEqual(
            [club["team_name"] for club in current["multi-source"]["previous_clubs"]],
            ["מכבי חיפה", "מכבי תל אביב"],
        )
        self.assertEqual(current["only-hapoel"]["status"], "no_previous_history")

    def test_same_season_is_ambiguous_and_response_is_stable_json(self):
        first = self.data.player_movements()
        second = self.data.player_movements()
        self.assertEqual(first, second)
        current = {entry["player_id"]: entry for entry in first["current_players"]}
        self.assertEqual(current["ambiguous"]["status"], "same_season_ambiguous")
        self.assertEqual(current["ambiguous"]["previous_clubs"], [])
        self.assertGreaterEqual(first["summary"]["ambiguous"], 1)
        response = create_app(self.data_dir).test_client().get("/api/player-movements")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("2085", payload["club"]["team_ids"])
        self.assertFalse(any(math.isnan(value) for value in _walk_numbers(payload) if isinstance(value, float)))

    def test_missing_history_file_degrades_to_recent_rows(self):
        (self.data_dir / "player_history.csv").unlink()
        data = CsvScoutingData(self.data_dir)
        report = data.player_movements()
        self.assertIn("arrival", {entry["player_id"] for entry in report["current_players"]})


def _walk_numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_numbers(item)
    else:
        yield value


if __name__ == "__main__":
    unittest.main()
