"""Scrape which club each player turned out for, including kids teams.

The main scrape covers three youth seasons in full detail. A scout also wants to see
where a player came from — ילדים and טרום as well as נערים — so this module walks the
same league and squad endpoints over every season that still has those tables and keeps
only the identity of each appearance. Goals and minutes are deliberately dropped.

    python -m ifa_scraper.history
"""

import argparse
import logging
import time

from . import config, leagues
from .client import IFAClient
from .run import (
    phase1_collect_teams,
    phase2_collect_squads,
    single_run_lock,
    write_csv,
)

log = logging.getLogger("ifa_scraper")

HISTORY_COLUMNS = [
    "player_id",
    "player_name",
    "season_id",
    "season",
    "team_id",
    "team_name",
    "age_group",
    "league_name",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=sorted({**config.SEASONS, **config.HISTORY_SEASONS}, reverse=True),
        help="season ids to scrape (default: current stats seasons plus history seasons)",
    )
    parser.add_argument("--no-cache", action="store_true", help="bypass the disk cache")
    parser.add_argument(
        "--leagues-from-config",
        action="store_true",
        help="use the configured league table instead of discovering each season's",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )

    known_seasons = {**config.SEASONS, **config.HISTORY_SEASONS}
    seasons = [s for s in args.seasons if s in known_seasons]
    if not seasons:
        parser.error(
            f"no known seasons given; choose from {sorted(known_seasons)}"
        )

    started = time.time()

    # The same lock as the main scrape: both hit the same origin, and sharing it keeps
    # a history run from competing with a stats run for the connection pool.
    with single_run_lock(config.DATA_DIR / ".scrape.lock"):
        client = IFAClient(use_cache=not args.no_cache)

        log.info(
            "scraping club history for: %s",
            ", ".join(known_seasons[s] for s in seasons),
        )
        # Discovered per season rather than taken from config: the older seasons ran
        # second divisions that the site's navigation no longer lists anywhere, and
        # they hold about a fifth of the appearances in those years.
        league_index = (
            None if args.leagues_from_config else leagues.youth_index(client, seasons)
        )
        team_seasons, empty_leagues = phase1_collect_teams(client, seasons, league_index)
        rows = phase2_collect_squads(client, team_seasons)

        # Newest first within each player, so the file reads the way the history panel
        # displays it.
        rows.sort(key=lambda r: (r["player_id"], -r["season_id"], r["team_name"]))
        write_csv(config.DATA_DIR / "player_history.csv", HISTORY_COLUMNS, rows)

    log.info(
        "done in %.1f min | %s rows, %s players, %s teams, %s team-seasons | requests: %s",
        (time.time() - started) / 60,
        len(rows),
        len({row["player_id"] for row in rows}),
        len({row["team_id"] for row in rows}),
        len(team_seasons),
        client.stats,
    )
    if empty_leagues:
        log.warning("%s league-seasons returned no teams", len(empty_leagues))
        for league_id, name, season_id in empty_leagues:
            log.warning(
                "  empty: %s (%s) season %s", name, league_id, config.season_label(season_id)
            )


if __name__ == "__main__":
    main()
