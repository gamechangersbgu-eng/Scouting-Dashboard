"""Masked-name merge policy: known names must never be displaced by a mask."""

import unittest

from ifa_scraper import name_quality, run


class NameQualityTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(name_quality.classify_name("Yosef Cohen"), name_quality.KNOWN)
        self.assertEqual(name_quality.classify_name("*******"), name_quality.MASKED)
        self.assertEqual(name_quality.classify_name("**"), name_quality.MASKED)
        self.assertEqual(name_quality.classify_name(""), name_quality.MISSING)
        self.assertEqual(name_quality.classify_name(None), name_quality.MISSING)
        self.assertEqual(name_quality.classify_name("nan"), name_quality.MISSING)

    def test_best_name_prefers_known_over_masked_regardless_of_order(self):
        status, name = name_quality.best_name(["*******", "Yosef Cohen", "*******"])
        self.assertEqual((status, name), (name_quality.KNOWN, "Yosef Cohen"))

    def test_best_name_newest_first_tie_break(self):
        # Two known names: the first (newest, by convention) wins the tie.
        status, name = name_quality.best_name(["Yosef Cohen", "Yossi Cohen"])
        self.assertEqual((status, name), (name_quality.KNOWN, "Yosef Cohen"))

    def test_best_name_all_masked_or_missing(self):
        status, name = name_quality.best_name(["*******", "", None])
        self.assertEqual(status, name_quality.MASKED)
        self.assertEqual(name, "*******")

        status, name = name_quality.best_name(["", None])
        self.assertEqual(status, name_quality.MISSING)


def _season_row(player_id, season_id, player_name, **extra):
    row = {
        "player_id": player_id,
        "player_name": player_name,
        "season_id": season_id,
        "season": f"20{season_id}/xx",
        "team_id": "t1",
        "team_name": "Team",
        "league_id": "101",
        "league_name": "League",
        "age_group": "נוער",
        "games": 1,
        "goals": 0,
        "minutes": 60,
        "starts": 1,
        "sub_on": 0,
        "sub_off": 0,
        "yellow_cards_league_cup": 0,
        "yellow_cards_toto": 0,
        "red_cards": 0,
    }
    row.update(extra)
    return row


class AggregatePlayersNameTests(unittest.TestCase):
    def test_masked_newest_row_does_not_overwrite_known_older_name(self):
        rows = [
            _season_row("p1", 26, "*******"),  # newest season, masked on this endpoint
            _season_row("p1", 25, "Yosef Cohen"),  # older season, real name published
        ]
        players = run.aggregate_players(rows, splits={}, details={})
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["player_name"], "Yosef Cohen")
        self.assertEqual(players[0]["name_status"], name_quality.KNOWN)

    def test_known_newest_name_still_wins_over_older_known_name(self):
        rows = [
            _season_row("p1", 26, "Yosef Cohen"),
            _season_row("p1", 25, "Y. Cohen"),
        ]
        players = run.aggregate_players(rows, splits={}, details={})
        self.assertEqual(players[0]["player_name"], "Yosef Cohen")
        self.assertEqual(players[0]["name_status"], name_quality.KNOWN)

    def test_all_masked_reports_masked_status(self):
        rows = [_season_row("p1", 26, "*******")]
        players = run.aggregate_players(rows, splits={}, details={})
        self.assertEqual(players[0]["player_name"], "*******")
        self.assertEqual(players[0]["name_status"], name_quality.MASKED)


if __name__ == "__main__":
    unittest.main()
