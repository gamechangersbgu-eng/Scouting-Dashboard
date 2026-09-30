"""Orchestrates the three scrape phases and writes the player CSVs."""

import argparse
import csv
import logging
import os
import time
from contextlib import contextmanager
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import config, leagues, name_quality, parse
from .client import IFAClient

log = logging.getLogger("ifa_scraper")

# Every tracked youth league name contains one of these, and so do the youth cups.
# Used to exclude senior competitions from a player's season game feed.
YOUTH_MARKERS = ("נוער", "נערים")

PLAYER_COLUMNS = [
    "player_id",
    "player_name",
    "name_status",
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

DETAIL_COLUMNS = ["player_id", "birth_year", "birth_month", "image_url", "details_fetched_at"]
_DETAIL_REQUIRED_COLUMNS = DETAIL_COLUMNS[:4]

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
                        # A team-season can be discovered under more than one
                        # league listing (see the "leagues" field below), so
                        # this stays keyed by league_id rather than being a
                        # second, independently-built set: keeping age_group
                        # and league_name attached to the league_id they were
                        # actually observed with is what lets phase2 emit them
                        # in matching order, instead of two comma-joined
                        # strings sorted independently that can silently
                        # drift out of alignment with each other.
                        "leagues": {},
                    },
                )
                if team_name and not entry["team_name"]:
                    entry["team_name"] = team_name
                entry["leagues"][str(league_id)] = {
                    "league_name": league_name,
                    "age_group": age_group,
                }
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
            # Iterate the leagues this team-season was actually discovered under
            # in one consistent order (numeric league_id) and build all three
            # display strings from that single ordering, so position i of
            # league_id always names the same league as position i of
            # league_name/age_group -- building them as separately-sorted sets
            # (the previous approach) can put unrelated leagues' id and name at
            # the same position once a team-season spans more than one league.
            ordered_league_ids = sorted(meta["leagues"], key=int)
            age_group = ", ".join(
                dict.fromkeys(meta["leagues"][lid]["age_group"] for lid in ordered_league_ids)
            )
            league_id = ", ".join(ordered_league_ids)
            league_name = ", ".join(
                dict.fromkeys(meta["leagues"][lid]["league_name"] for lid in ordered_league_ids)
            )
            for player in squad:
                rows.append(
                    {
                        **player,
                        "season_id": season_id,
                        "season": config.season_label(season_id),
                        "team_id": team_id,
                        "team_name": meta["team_name"],
                        "age_group": age_group,
                        "league_id": league_id,
                        "league_name": league_name,
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
    """Read cached player-card fields, including inactive historical players."""
    if not path.exists():
        return {}
    known = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        # A checkpoint written before a column existed is incomplete, so ignore it
        # and let those players be fetched again.
        if not set(_DETAIL_REQUIRED_COLUMNS).issubset(reader.fieldnames or []):
            return {}
        for row in reader:
            year = row.get("birth_year") or ""
            month = row.get("birth_month") or ""
            known[row["player_id"]] = {
                "birth_year": int(year) if year.isdigit() else None,
                "birth_month": int(month) if month.isdigit() else None,
                "image_url": row.get("image_url") or None,
                "details_fetched_at": row.get("details_fetched_at") or None,
            }
    return known


def detail_refresh_player_ids(season_rows, active_season_ids=None):
    """Return player IDs observed in the current or previous detailed season."""
    active_season_ids = set(active_season_ids or config.active_detail_season_ids())
    return sorted(
        {
            row["player_id"]
            for row in season_rows
            if row.get("season_id") in active_season_ids and row.get("player_id")
        }
    )


def _details_are_stale(detail, now, ttl_days):
    fetched_at = detail.get("details_fetched_at")
    if not fetched_at:
        return True
    try:
        timestamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp < now - timedelta(days=ttl_days)


def _checkpoint_needs_timestamp_migration(path):
    if not path.exists():
        return False
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return "details_fetched_at" not in (csv.DictReader(handle).fieldnames or [])


def _rewrite_detail_checkpoint(path, details):
    """Atomically persist one canonical row per player without losing inactive rows."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_COLUMNS)
        writer.writeheader()
        for player_id in sorted(details):
            detail = details[player_id]
            writer.writerow(
                {
                    "player_id": player_id,
                    "birth_year": detail["birth_year"] or "",
                    "birth_month": detail["birth_month"] or "",
                    "image_url": detail["image_url"] or "",
                    "details_fetched_at": detail.get("details_fetched_at") or "",
                }
            )
    os.replace(temporary_path, path)


def _migrate_detail_checkpoint_timestamps(path, details):
    """Timestamp legacy cached rows once so they remain reusable after restart.

    A legacy checkpoint proves the detail was successfully parsed and written,
    but predates per-row timestamps.  Its modification time is the best durable
    lower bound we have for that work.  This avoids re-fetching every valid
    active row merely because the timestamp feature was added later, while
    leaving all existing explicit timestamps unchanged.
    """
    migrated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    for detail in details.values():
        if not detail.get("details_fetched_at"):
            detail["details_fetched_at"] = migrated_at
    _rewrite_detail_checkpoint(path, details)
    log.info("phase 4: migrated %s cached detail rows to timestamped checkpoint", len(details))


def phase4_player_details(
    client,
    player_ids,
    checkpoint_path,
    ttl_days=None,
    now=None,
    checkpoint_batch_size=None,
):
    """Fetch birth date and photo URL for each player from their player page.

    Player pages are ~150KB and are not stored in the HTTP cache.  This CSV is
    therefore the durable checkpoint: every successful result is merged by
    player_id and atomically rewritten periodically and on interruption.
    """
    details = load_detail_checkpoint(checkpoint_path)
    ttl_days = config.PLAYER_DETAILS_TTL_DAYS if ttl_days is None else ttl_days
    now = now or datetime.now(timezone.utc)
    checkpoint_batch_size = (
        config.PLAYER_DETAILS_CHECKPOINT_BATCH_SIZE
        if checkpoint_batch_size is None
        else checkpoint_batch_size
    )
    if checkpoint_batch_size < 1:
        raise ValueError("checkpoint_batch_size must be positive")

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if _checkpoint_needs_timestamp_migration(checkpoint_path):
        _migrate_detail_checkpoint_timestamps(checkpoint_path, details)

    player_ids = list(dict.fromkeys(player_ids))
    pending = [
        player_id
        for player_id in player_ids
        if player_id not in details or _details_are_stale(details[player_id], now, ttl_days)
    ]
    fresh_count = len(player_ids) - len(pending)
    log.info(
        "phase 4: %s active players considered; %s fresh details skipped; %s due for refresh; %s cached rows retained",
        len(player_ids), fresh_count, len(pending), len(details),
    )
    if not pending:
        return details

    def fetch(player_id):
        page = client.player_page(player_id)
        if page is None:
            return player_id, None
        return player_id, parse.parse_player_details(page)

    completed = 0
    received_since_checkpoint = 0
    pool = ThreadPoolExecutor(config.MAX_WORKERS)
    futures = {pool.submit(fetch, player_id): player_id for player_id in pending}
    try:
        # as_completed is essential here: pool.map yields in submission order,
        # so one slow early page can otherwise strand later successful pages in
        # memory until it returns, making Ctrl+C lose useful completed work.
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                fetched_player_id, result = future.result()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.warning("phase 4: player %s detail fetch failed: %s", player_id, exc)
                continue
            if result is None:
                log.warning("phase 4: player %s detail page was unavailable; will retry later", fetched_player_id)
                continue

            details[fetched_player_id] = {
                **result,
                "details_fetched_at": now.isoformat(),
            }
            completed += 1
            received_since_checkpoint += 1
            if received_since_checkpoint >= checkpoint_batch_size:
                _rewrite_detail_checkpoint(checkpoint_path, details)
                received_since_checkpoint = 0
                log.info("phase 4: checkpointed %s/%s successful detail pages", completed, len(pending))
    except KeyboardInterrupt:
        # Persist everything already received from completed futures, then let
        # the caller stop the pipeline.  Remaining futures are not treated as
        # completed and will be selected by player_id on the next run.
        _rewrite_detail_checkpoint(checkpoint_path, details)
        log.warning("phase 4 interrupted: checkpointed %s/%s successful detail pages", completed, len(pending))
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    # Final durable flush covers a short final batch and keeps the checkpoint
    # canonical (one row per player_id) even after repeated refreshes.
    _rewrite_detail_checkpoint(checkpoint_path, details)
    log.info("phase 4: checkpointed %s/%s successful detail pages", completed, len(pending))

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
        # rows[0] is the newest appearance, but IFA masks some players' names as a
        # run of asterisks on some pages while publishing the real name elsewhere
        # for the same player. Using rows[0]'s name unconditionally can silently
        # replace a known name with a masked one on the very next scrape, so every
        # observed name is ranked instead (known > masked > missing; newest wins
        # ties, since ``rows`` is already sorted newest-first).
        name_status, player_name = name_quality.best_name(row["player_name"] for row in rows)
        players.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "name_status": name_status,
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


def main(argv=None, data_dir=None):
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
    args = parser.parse_args(argv)
    data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )

    seasons = [s for s in args.seasons if s in config.SEASONS]
    if not seasons:
        parser.error(f"no known seasons given; choose from {sorted(config.SEASONS)}")

    started = time.time()

    with single_run_lock(data_dir / ".scrape.lock"):
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
            player_ids = detail_refresh_player_ids(season_rows)
            details = phase4_player_details(
                client, player_ids, data_dir / "player_details.csv"
            )

        players = aggregate_players(season_rows, splits, details)

        write_csv(data_dir / "player_season_stats.csv", SEASON_COLUMNS, season_rows)
        write_csv(data_dir / "players_youth.csv", PLAYER_COLUMNS, players)

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
