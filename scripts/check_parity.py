"""Compare CsvScoutingData and PostgresScoutingData on the same live dataset.

This is the gate the user set before Render's DATA_SOURCE may ever become
``postgres``: it must be run, and pass, against a Postgres dataset that was
imported from the *same* data/ directory currently backing the CSV-driven
production app, before that cutover is even considered. A clean run of this
script is necessary but the implementation report is explicit that it is not
sufficient on its own -- see "CSV/Postgres parity results" there for exactly
what this script could and could not verify without a real DATABASE_URL.

Always compares player_id 206865 and 249469 (the two players named in this
task as known past regressions) in addition to a random sample, so a fix
regressing either of them again is never masked by sampling variance.

Usage:
    DATABASE_URL=postgresql://... python -m scripts.check_parity
    DATABASE_URL=postgresql://... python -m scripts.check_parity --sample-size 200
"""

import argparse
import logging
import random
from pathlib import Path

from dashboard.app_core import CsvScoutingData

log = logging.getLogger("scripts.check_parity")

ALWAYS_CHECK_PLAYER_IDS = ["206865", "249469"]

# Fields compared directly on the player() payload. totals/seasons/teams/venues
# are compared structurally below rather than listed here, since they are
# nested and a raw != would report the whole structure instead of what
# actually differs.
SCALAR_FIELDS = [
    "player_name",
    "birth_year",
    "current_team",
    "num_teams",
    "age_groups",
    "leagues",
    "seasons_played",
    "plays_above_age",
    "age_groups_above",
    "first_club",
    "first_club_city",
    "likely_origin_city",
    "likely_origin_confidence",
]


def _diff_scalars(csv_player, pg_player, player_id, mismatches):
    for field in SCALAR_FIELDS:
        if csv_player.get(field) != pg_player.get(field):
            mismatches.append(
                f"player {player_id}: {field} differs: csv={csv_player.get(field)!r} "
                f"postgres={pg_player.get(field)!r}"
            )


def _diff_totals(csv_player, pg_player, player_id, mismatches):
    if csv_player["totals"] != pg_player["totals"]:
        mismatches.append(
            f"player {player_id}: totals differ: csv={csv_player['totals']} postgres={pg_player['totals']}"
        )


def _diff_seasons(csv_player, pg_player, player_id, mismatches):
    csv_seasons = {(s["season_id"], s["team_id"]): s for s in csv_player["seasons"]}
    pg_seasons = {(s["season_id"], s["team_id"]): s for s in pg_player["seasons"]}
    if csv_seasons.keys() != pg_seasons.keys():
        mismatches.append(
            f"player {player_id}: season/team keys differ: "
            f"csv_only={sorted(csv_seasons.keys() - pg_seasons.keys())} "
            f"postgres_only={sorted(pg_seasons.keys() - csv_seasons.keys())}"
        )
        return
    for key, csv_season in csv_seasons.items():
        pg_season = pg_seasons[key]
        for field in ("games", "goals", "minutes", "starts", "sub_on", "sub_off", "stats_available"):
            if csv_season.get(field) != pg_season.get(field):
                mismatches.append(
                    f"player {player_id} season {key}: {field} differs: "
                    f"csv={csv_season.get(field)!r} postgres={pg_season.get(field)!r}"
                )


def compare_player(csv_data, pg_data, player_id, mismatches):
    csv_player = csv_data.player(player_id)
    pg_player = pg_data.player(player_id)
    if csv_player is None and pg_player is None:
        return
    if csv_player is None or pg_player is None:
        mismatches.append(f"player {player_id}: present in only one source (csv={csv_player is not None}, postgres={pg_player is not None})")
        return
    _diff_scalars(csv_player, pg_player, player_id, mismatches)
    _diff_totals(csv_player, pg_player, player_id, mismatches)
    _diff_seasons(csv_player, pg_player, player_id, mismatches)


def compare_summary(csv_data, pg_data, mismatches):
    csv_summary = csv_data.summary()
    pg_summary = pg_data.summary()
    # This compares BaseScoutingData.summary() directly, not the league/location
    # filtering dashboard.app layers on top at Flask-app construction time --
    # that is exercised separately (see the "leagues" note in the report).
    for field in ("players", "player_seasons", "above_age"):
        if csv_summary.get(field) != pg_summary.get(field):
            mismatches.append(f"summary: {field} differs: csv={csv_summary.get(field)!r} postgres={pg_summary.get(field)!r}")


def compare_search(csv_data, pg_data, queries, mismatches):
    for query in queries:
        csv_ids = {p["player_id"] for p in csv_data.search(query, limit=1000)}
        pg_ids = {p["player_id"] for p in pg_data.search(query, limit=1000)}
        if csv_ids != pg_ids:
            mismatches.append(
                f"search({query!r}): result sets differ: csv_only={sorted(csv_ids - pg_ids)[:10]} "
                f"postgres_only={sorted(pg_ids - csv_ids)[:10]}"
            )


def run_parity_check(data_dir=None, database_url=None, sample_size=50, seed=0):
    # Imported lazily: only this entry point needs psycopg, so importing the
    # module itself never requires it.
    from dashboard.postgres_source import PostgresScoutingData

    data_dir = Path(data_dir) if data_dir is not None else None
    csv_data = CsvScoutingData(data_dir)
    pg_data = PostgresScoutingData(database_url)

    all_ids = list(csv_data.players.keys())
    rng = random.Random(seed)
    sample = set(ALWAYS_CHECK_PLAYER_IDS)
    sample.update(rng.sample(all_ids, min(sample_size, len(all_ids))))

    mismatches = []
    compare_summary(csv_data, pg_data, mismatches)
    for player_id in sorted(sample):
        compare_player(csv_data, pg_data, player_id, mismatches)

    sample_names = [csv_data.players[pid]["player_name"] for pid in sample if pid in csv_data.players]
    queries = [""] + [name.split()[0] for name in sample_names[:5] if name]
    compare_search(csv_data, pg_data, queries, mismatches)

    return mismatches, len(sample)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    mismatches, sample_size = run_parity_check(data_dir=args.data_dir, sample_size=args.sample_size, seed=args.seed)

    log.info("compared %s players (including %s)", sample_size, ALWAYS_CHECK_PLAYER_IDS)
    if mismatches:
        log.error("%s mismatches found:", len(mismatches))
        for mismatch in mismatches:
            log.error("  - %s", mismatch)
        raise SystemExit(1)
    log.info("no mismatches found")


if __name__ == "__main__":
    main()
