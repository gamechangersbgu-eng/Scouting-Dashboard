"""Orchestrates the three scrape phases and writes the player CSVs."""

import argparse
import csv
import logging
import os
import time
from contextlib import contextmanager
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal

from . import config, leagues, parse
from .client import IFAClient

log = logging.getLogger("ifa_scraper")

# Every tracked youth league name contains one of these, and so do the youth cups.
# Used to exclude senior competitions from a player's season game feed.
YOUTH_MARKERS = ("נוער", "נערים")

PLAYER_COLUMNS = [
    "player_id",
    "player_name",
    "birth_year",
    "current_team",
    "teams_history",
    "num_teams",
    "age_groups",
    "plays_above_age",
    "age_groups_above",
    "above_age_history",
    "leagues",
    "seasons",
    "goals_total",
    "goals_league",
    "goals_cup",
    "games_total",
    "minutes_total",
    "avg_minutes_per_game",
    "starts",
    "sub_on",
    "sub_off",
    "yellow_cards_league_cup",
    "yellow_cards_toto",
    "yellow_cards_total",
    "red_cards",
    "image_url",
]

DETAIL_COLUMNS = ["player_id", "birth_year", "birth_month", "image_url"]

SEASON_COLUMNS = [
    "player_id",
    "player_name",
    "season_id",
    "season",
    "team_id",
    "team_name",
    "league_id",
    "age_group",
    "league_name",
    "games",
    "goals",
    "minutes",
    "starts",
    "sub_on",
    "sub_off",
    "yellow_cards_league_cup",
    "yellow_cards_toto",
    "red_cards",
    "stats_available",
    "stats_source",
    "stats_completeness",
]


@contextmanager
def single_run_lock(path):
    """Refuse to start if another run is active, since they share checkpoint files.

    Two concurrent runs append to the same checkpoint and interleave rows, so the
    lock protects data integrity as well as being polite to the origin.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            existing = path.read_text().strip()
        except OSError:
            existing = "unknown"
        stale = True
        if existing.isdigit():
            try:
                os.kill(int(existing), 0)
                stale = False
            except (OSError, ProcessLookupError):
                stale = True
        if not stale:
            raise SystemExit(
                f"another scrape is already running (pid {existing}); "
                f"wait for it to finish or remove {path}"
            )
        log.warning("clearing stale lock from pid %s", existing)
        path.unlink(missing_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        path.unlink(missing_ok=True)


def is_youth_competition(name):
    return any(marker in name for marker in YOUTH_MARKERS)


def round_half_up(value, places=1):
    """Round away from zero on ties, so 75.35 -> 75.4 rather than banker's 75.3."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def phase1_collect_teams(client, seasons, leagues_by_season=None):
    """Map every (team_id, season_id) in the youth leagues to its name and league.

    leagues_by_season lets a caller pass the league index discovered for each season,
    which is not the same set every year; without it the configured table is used for
    every season.
    """
    fallback = config.league_index()
    leagues_by_season = leagues_by_season or {}
    season_leagues = {sid: leagues_by_season.get(sid) or fallback for sid in seasons}
    jobs = [(lid, sid) for sid in seasons for lid in season_leagues[sid]]
    team_seasons = {}
    empty_leagues = []

    def fetch(job):
        league_id, season_id = job
        return job, leagues.league_teams(client, league_id, season_id)

    with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
        for index, ((league_id, season_id), teams) in enumerate(pool.map(fetch, jobs), 1):
            age_group, league_name = season_leagues[season_id][league_id]
            if not teams:
                empty_leagues.append((league_id, league_name, season_id))
            for team_id, team_name in teams.items():
                entry = team_seasons.setdefault(
                    (team_id, season_id),
                    {
                        "team_name": team_name,
                        "age_groups": set(),
                        "league_ids": set(),
                        "league_names": set(),
                    },
                )
                if team_name and not entry["team_name"]:
                    entry["team_name"] = team_name
                entry["age_groups"].add(age_group)
                entry["league_ids"].add(str(league_id))
                entry["league_names"].add(league_name)
            if index % 25 == 0 or index == len(jobs):
                log.info("phase 1: %s/%s league-seasons, %s team-seasons found",
                         index, len(jobs), len(team_seasons))

    return team_seasons, empty_leagues


def phase2_collect_squads(client, team_seasons):
    """Fetch each team-season squad, producing one row per player-team-season."""
    jobs = sorted(team_seasons)
    rows = []

    def fetch(job):
        team_id, season_id = job
        return job, parse.parse_squad_stats(client.team_player_stats(team_id, season_id))

    with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
        for index, ((team_id, season_id), squad) in enumerate(pool.map(fetch, jobs), 1):
            meta = team_seasons[(team_id, season_id)]
            for player in squad:
                rows.append(
                    {
                        **player,
                        "season_id": season_id,
                        "season": config.season_label(season_id),
                        "team_id": team_id,
                        "team_name": meta["team_name"],
                        "age_group": ", ".join(sorted(meta["age_groups"])),
                        "league_id": ", ".join(sorted(meta["league_ids"], key=int)),
                        "league_name": ", ".join(sorted(meta["league_names"])),
                    }
                )
            if index % 100 == 0 or index == len(jobs):
                log.info("phase 2: %s/%s team-seasons, %s squad rows",
                         index, len(jobs), len(rows))

    return rows


def phase3_goal_splits(client, season_rows):
    """Split goals into league vs cup, only for player-seasons that produced goals."""
    goals_by_player_season = defaultdict(int)
    for row in season_rows:
        goals_by_player_season[(row["player_id"], row["season_id"])] += row["goals"] or 0

    jobs = sorted(key for key, goals in goals_by_player_season.items() if goals > 0)
    log.info("phase 3: %s player-seasons with goals (of %s total)",
             len(jobs), len(goals_by_player_season))

    splits = {}

    def fetch(job):
        player_id, season_id = job
        games = parse.parse_player_games(client.player_games(player_id, season_id))
        youth_games = [g for g in games if is_youth_competition(g["competition"])]
        return job, parse.split_goals_by_competition(youth_games)

    with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
        for index, (job, split) in enumerate(pool.map(fetch, jobs), 1):
            splits[job] = split
            if index % 500 == 0 or index == len(jobs):
                log.info("phase 3: %s/%s player-seasons split", index, len(jobs))

    return splits


def natural_age_groups(season_rows, details):
    """Find where each birth-year cohort normally plays, per season.

    Youth brackets overlap: a given bracket is mostly one birth year plus a slice of
    the year below. So a player's own cohort defines what "at age" means for him, and
    the natural bracket is taken as the one holding most of his birth year that season.
    """
    counts = defaultdict(lambda: defaultdict(int))
    for row in season_rows:
        detail = details.get(row["player_id"]) or {}
        birth_year = detail.get("birth_year")
        if not birth_year or row["age_group"] not in config.AGE_GROUP_LADDER:
            continue
        counts[(birth_year, row["season_id"])][row["age_group"]] += 1

    natural = {}
    for key, groups in counts.items():
        natural[key] = max(groups.items(), key=lambda kv: kv[1])[0]
    return natural


def age_groups_above(row, details, natural):
    """Steps up the age ladder for one player-team-season, or None if not determinable."""
    detail = details.get(row["player_id"]) or {}
    birth_year = detail.get("birth_year")
    actual = row["age_group"]
    if not birth_year or actual not in config.AGE_GROUP_LADDER:
        return None
    expected = natural.get((birth_year, row["season_id"]))
    if expected is None:
        return None
    return config.AGE_GROUP_LADDER.index(actual) - config.AGE_GROUP_LADDER.index(expected)


def load_detail_checkpoint(path):
    """Read previously scraped player-card fields so reruns skip completed work."""
    if not path.exists():
        return {}
    known = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        # A checkpoint written before a column existed is incomplete, so ignore it
        # and let those players be fetched again.
        if not set(DETAIL_COLUMNS).issubset(reader.fieldnames or []):
            return {}
        for row in reader:
            year = row.get("birth_year") or ""
            month = row.get("birth_month") or ""
            known[row["player_id"]] = {
                "birth_year": int(year) if year.isdigit() else None,
                "birth_month": int(month) if month.isdigit() else None,
                "image_url": row.get("image_url") or None,
            }
    return known


def phase4_player_details(client, player_ids, checkpoint_path):
    """Fetch birth date and photo URL for each player from their player page.

    Player pages are ~150KB and only a few fields are needed, so the extracted values
    are appended to a small checkpoint file rather than caching the raw HTML.
    """
    details = load_detail_checkpoint(checkpoint_path)
    pending = [pid for pid in player_ids if pid not in details]
    log.info(
        "phase 4: %s player pages to fetch (%s already known)", len(pending), len(details)
    )
    if not pending:
        return details

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not details

    def fetch(player_id):
        page = client.player_page(player_id)
        if page is None:
            return player_id, None
        return player_id, parse.parse_player_details(page)

    mode = "a" if details else "w"
    with checkpoint_path.open(mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_COLUMNS)
        if write_header:
            writer.writeheader()
        with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
            for index, (player_id, result) in enumerate(pool.map(fetch, pending), 1):
                # A failed fetch is left unrecorded so a later run retries it.
                if result is not None:
                    details[player_id] = result
                    writer.writerow(
                        {
                            "player_id": player_id,
                            "birth_year": result["birth_year"] or "",
                            "birth_month": result["birth_month"] or "",
                            "image_url": result["image_url"] or "",
                        }
                    )
                if index % 1000 == 0 or index == len(pending):
                    handle.flush()
                    log.info("phase 4: %s/%s player pages", index, len(pending))

    return details


def aggregate_players(season_rows, splits, details=None):
    """Collapse player-team-season rows into one row per player_id."""
    details = details or {}
    natural = natural_age_groups(season_rows, details)

    by_player = defaultdict(list)
    for row in season_rows:
        by_player[row["player_id"]].append(row)

    players = []
    for player_id, rows in by_player.items():
        # Newest season first so team history reads current -> first.
        rows.sort(key=lambda r: (-r["season_id"], r["team_name"]))

        totals = defaultdict(int)
        for row in rows:
            for field in (
                "games",
                "goals",
                "minutes",
                "starts",
                "sub_on",
                "sub_off",
                "yellow_cards_league_cup",
                "yellow_cards_toto",
                "red_cards",
            ):
                totals[field] += row[field] or 0

        goals_league = goals_cup = 0
        for season_id in {row["season_id"] for row in rows}:
            split = splits.get((player_id, season_id))
            if split:
                goals_league += split[parse.LEAGUE]
                goals_cup += split[parse.CUP] + split[parse.TOTO]

        # De-duplicate team spells while preserving current -> first order.
        history = []
        for row in rows:
            label = f"{row['team_name']} ({row['season']})"
            if label not in history:
                history.append(label)

        teams_seen = []
        for row in rows:
            if row["team_name"] and row["team_name"] not in teams_seen:
                teams_seen.append(row["team_name"])

        age_groups = []
        leagues = []
        seasons = []
        for row in rows:
            for value in row["age_group"].split(", "):
                if value and value not in age_groups:
                    age_groups.append(value)
            for value in row["league_name"].split(", "):
                if value and value not in leagues:
                    leagues.append(value)
            if row["season"] not in seasons:
                seasons.append(row["season"])

        # Rows are already newest-first, so the latest season describes the current spell.
        latest_season = rows[0]["season_id"]
        above_now = 0
        above_history = []
        for row in rows:
            steps = age_groups_above(row, details, natural)
            if steps is None or steps <= 0:
                continue
            if row["season_id"] == latest_season:
                above_now = max(above_now, steps)
            label = f"{row['age_group']} {row['season']} (+{steps})"
            if label not in above_history:
                above_history.append(label)

        games = totals["games"]
        detail = (details or {}).get(player_id) or {}
        players.append(
            {
                "player_id": player_id,
                "player_name": rows[0]["player_name"],
                "birth_year": detail.get("birth_year") or "",
                "image_url": detail.get("image_url") or "",
                "current_team": teams_seen[0] if teams_seen else "",
                "teams_history": " | ".join(history),
                "num_teams": len(teams_seen),
                "age_groups": ", ".join(age_groups),
                "plays_above_age": "Yes" if above_now > 0 else "No",
                "age_groups_above": above_now,
                "above_age_history": " | ".join(above_history),
                "leagues": ", ".join(leagues),
                "seasons": ", ".join(seasons),
                "goals_total": totals["goals"],
                "goals_league": goals_league,
                "goals_cup": goals_cup,
                "games_total": games,
                "minutes_total": totals["minutes"],
                "avg_minutes_per_game": round_half_up(totals["minutes"] / games) if games else 0,
                "starts": totals["starts"],
                "sub_on": totals["sub_on"],
                "sub_off": totals["sub_off"],
                "yellow_cards_league_cup": totals["yellow_cards_league_cup"],
                "yellow_cards_toto": totals["yellow_cards_toto"],
                "yellow_cards_total": totals["yellow_cards_league_cup"]
                + totals["yellow_cards_toto"],
                "red_cards": totals["red_cards"],
            }
        )

    players.sort(key=lambda p: (-p["goals_total"], -p["minutes_total"], p["player_id"]))
    return players


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel renders the Hebrew columns correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("wrote %s rows -> %s", len(rows), path)


def main():
    parser = argparse.ArgumentParser(description="Scrape IFA youth league player stats.")
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=sorted(config.SEASONS, reverse=True),
        help="season ids to scrape (default: all configured)",
    )
    parser.add_argument("--no-cache", action="store_true", help="bypass the disk cache")
    parser.add_argument(
        "--skip-goal-split",
        action="store_true",
        help="skip phase 3; leaves goals_league/goals_cup at 0",
    )
    parser.add_argument(
        "--skip-player-details",
        action="store_true",
        help="skip phase 4; leaves birth_year and image_url empty",
    )
    parser.add_argument(
        "--leagues-from-config",
        action="store_true",
        help="use the configured league table instead of discovering each season's",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )

    seasons = [s for s in args.seasons if s in config.SEASONS]
    if not seasons:
        parser.error(f"no known seasons given; choose from {sorted(config.SEASONS)}")

    started = time.time()

    with single_run_lock(config.DATA_DIR / ".scrape.lock"):
        client = IFAClient(use_cache=not args.no_cache)

        log.info("scraping seasons: %s", ", ".join(config.SEASONS[s] for s in seasons))
        league_index = (
            None
            if args.leagues_from_config
            else leagues.youth_index(client, seasons)
        )
        team_seasons, empty_leagues = phase1_collect_teams(client, seasons, league_index)
        season_rows = phase2_collect_squads(client, team_seasons)
        splits = {} if args.skip_goal_split else phase3_goal_splits(client, season_rows)

        details = {}
        if not args.skip_player_details:
            player_ids = sorted({row["player_id"] for row in season_rows})
            details = phase4_player_details(
                client, player_ids, config.DATA_DIR / "player_details.csv"
            )

        players = aggregate_players(season_rows, splits, details)

        write_csv(config.DATA_DIR / "player_season_stats.csv", SEASON_COLUMNS, season_rows)
        write_csv(config.DATA_DIR / "players_youth.csv", PLAYER_COLUMNS, players)

    log.info(
        "done in %.1f min | %s players, %s player-season rows, %s team-seasons | requests: %s",
        (time.time() - started) / 60,
        len(players),
        len(season_rows),
        len(team_seasons),
        client.stats,
    )
    if empty_leagues:
        log.warning("%s league-seasons returned no teams", len(empty_leagues))
        for league_id, name, season_id in empty_leagues:
            log.warning("  empty: %s (%s) season %s", name, league_id, config.SEASONS[season_id])


if __name__ == "__main__":
    main()
