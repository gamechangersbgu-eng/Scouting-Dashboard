"""phase1_collect_teams / phase2_collect_squads: league_id/league_name/age_group
must stay correctly paired when a team-season spans more than one league.

Real data confirms this happens and is not vanishingly rare: scanning the
repository's actual player_history.csv found 153 rows (out of 194,518) with a
multi-valued league_id -- e.g. player_id 151674, team_id 5613, season 2014/15,
league_id "182, 740" with two *different* age-group tournaments
("ילדים טרום א", "ילדים טרום ב"). The previous implementation built the
age_group/league_id/league_name display strings from three independently
sorted sets, so a team-season's Nth league_id and Nth league_name were not
guaranteed to describe the same league -- these tests pin down the fix:
building all three from one consistently-ordered structure.
"""

import unittest
from unittest import mock

from ifa_scraper import run


class Phase1LeaguePairingTests(unittest.TestCase):
    def test_team_discovered_under_two_leagues_keeps_id_name_age_group_paired(self):
        # League 740's name would sort BEFORE league 182's name alphabetically,
        # which is exactly the condition that silently misaligned the old
        # independently-sorted sets (league_id sorted numerically: 182 before
        # 740; league_name sorted as text: "Z-League" before "A-League").
        season_leagues = {
            16: {
                182: ("Age Group A", "A-League"),
                740: ("Age Group B", "Z-League"),
            }
        }

        def fake_league_teams(client, league_id, season_id):
            return {"5613": "Some Club"}

        with mock.patch.object(run.leagues, "league_teams", side_effect=fake_league_teams):
            team_seasons, empty_leagues = run.phase1_collect_teams(
                client=None, seasons=[16], leagues_by_season=season_leagues
            )

        self.assertEqual(empty_leagues, [])
        entry = team_seasons[("5613", 16)]
        self.assertEqual(entry["leagues"]["182"], {"league_name": "A-League", "age_group": "Age Group A"})
        self.assertEqual(entry["leagues"]["740"], {"league_name": "Z-League", "age_group": "Age Group B"})

    def test_phase2_emits_league_id_and_league_name_in_matching_order(self):
        team_seasons = {
            ("5613", 16): {
                "team_name": "Some Club",
                "leagues": {
                    "182": {"league_name": "A-League", "age_group": "Age Group A"},
                    "740": {"league_name": "Z-League", "age_group": "Age Group B"},
                },
            }
        }

        def fake_parse_squad_stats(page):
            return [{"player_id": "p1", "player_name": "Player One"}]

        with mock.patch.object(run.parse, "parse_squad_stats", side_effect=fake_parse_squad_stats), \
             mock.patch.object(run.config, "season_label", return_value="2014/15"):
            fake_client = mock.Mock()
            fake_client.team_player_stats.return_value = None
            rows = run.phase2_collect_squads(fake_client, team_seasons)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        # league_id "182, 740" (sorted numerically) must line up with
        # league_name "A-League, Z-League" -- position 0 is league 182 in both.
        self.assertEqual(row["league_id"], "182, 740")
        self.assertEqual(row["league_name"], "A-League, Z-League")
        self.assertEqual(row["age_group"], "Age Group A, Age Group B")

    def test_single_league_team_season_is_unaffected(self):
        team_seasons = {
            ("t1", 26): {
                "team_name": "Solo FC",
                "leagues": {"101": {"league_name": "Top League", "age_group": "Youth"}},
            }
        }

        def fake_parse_squad_stats(page):
            return [{"player_id": "p1", "player_name": "Player One"}]

        with mock.patch.object(run.parse, "parse_squad_stats", side_effect=fake_parse_squad_stats), \
             mock.patch.object(run.config, "season_label", return_value="2024/25"):
            fake_client = mock.Mock()
            fake_client.team_player_stats.return_value = None
            rows = run.phase2_collect_squads(fake_client, team_seasons)

        row = rows[0]
        self.assertEqual(row["league_id"], "101")
        self.assertEqual(row["league_name"], "Top League")
        self.assertEqual(row["age_group"], "Youth")


if __name__ == "__main__":
    unittest.main()
