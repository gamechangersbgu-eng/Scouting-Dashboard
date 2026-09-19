"""Finds out which youth leagues the association actually ran in a given season.

The hard-coded table in config mirrors the site's navigation, which lists only the
competitions the association currently promotes. That is not the same as the set that
ran in a past season: the youth brackets used to be split into second divisions such as
"ליגת נערים ג' מרכז 2" and "נערים ג' דרג 2 - דרום", and those appear nowhere in the
navigation, not even on a page requested for the season they ran in. Scraping a past
season from the navigation therefore drops roughly a fifth of its appearances.

So the index is discovered instead: the standings endpoint is asked for every league id
in a plausible range, the ids that answer with a table are named from their own page,
and the youth ones are kept. Results are checkpointed per season, so the scan is paid
for once.

    python -m ifa_scraper.leagues            # discover and print the index

Both scrapes use this index by default and fall back to the configured table with
--leagues-from-config. The navigation turned out to be incomplete for current seasons
too: it misses the two youth leagues that ran in 2024/25 and the development league that
started in 2026/27.
"""

import argparse
import csv
import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, parse
from .client import IFAClient

log = logging.getLogger("ifa_scraper")

# Every youth league id the site has ever served sits inside this range; the ids below
# it belong to the senior divisions and there is nothing above it.
SCAN_RANGE = range(100, 1000)

# How many fixture rounds to union when a league-season has no standings table.
# Kids divisions can run a long home-and-away, so this is well above a typical
# 12-team schedule; the idle-round stop keeps short leagues cheap.
FIXTURE_ROUNDS = 16
# Some kids leagues leave מחזור 1-2 blank and only start posting fixtures later.
# Probe this far before treating an empty placeholder as "this league didn't run".
MIN_BLANK_ROUNDS = 6

INDEX_COLUMNS = ["season_id", "league_id", "league_name", "age_group", "teams"]

_HEADING_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# League headings read "2019/2020ליגת נערים ג' מרכז", with the season glued on.
_SEASON_PREFIX_RE = re.compile(r"^\d{4}/\d{4}")


def _table_fragment(raw):
    return html.unescape(parse.unwrap_response(raw) or "").strip()


def league_teams(client, league_id, season_id):
    """Every team in one league-season, from the standings or else from the fixtures.

    The youngest brackets often run without standings: those leagues answer with a
    fixture list and no table, so the teams have to be read off the fixtures. One round
    does not name everybody, since squads have byes and postponements, so rounds are
    unioned until two in a row add nothing new. Kids leagues also sometimes leave the
    first rounds blank, so a leading placeholder is probed a few rounds deeper before
    it is treated as empty. A missing league returns no fragment at all and is skipped.
    """
    first = client.league_tables(league_id, season_id)
    teams = parse.parse_league_teams(first)
    if teams:
        return teams

    # No table: the same payload is often already the round-1 fixture list,
    # including kids leagues whose rows are plain divs rather than links.
    found = parse.parse_league_fixture_teams(first)
    frag = _table_fragment(first)
    if not found and not frag:
        return {}

    idle = 0
    for round_id in range(2, FIXTURE_ROUNDS + 1):
        extra = parse.parse_league_fixture_teams(
            client.league_tables(league_id, season_id, round_id=round_id)
        )
        before = len(found)
        found.update(extra)
        if not found:
            if round_id >= MIN_BLANK_ROUNDS:
                break
            continue
        idle = 0 if len(found) > before else idle + 1
        if idle >= 2:
            break
    return found


def league_name(client, league_id, season_id):
    """The league's own name for that season, read from its page heading."""
    page = client.league_page(league_id, season_id)
    if not page:
        return ""
    heading = _HEADING_RE.search(page)
    if not heading:
        return ""
    text = re.sub(r"\s+", " ", _TAG_RE.sub("", heading.group(1))).strip()
    return _SEASON_PREFIX_RE.sub("", text).strip()


# (token, canonical bracket), oldest first. Trome aliases have to be tried before
# "ילדים א/ב/ג", because "טרום ילדים ב'" contains the substring "ילדים ב".
_BRACKET_TOKENS = [
    ("נוער", "נוער"),
    ("נערים א", "נערים א"),
    ("נערים ב", "נערים ב"),
    ("נערים ג", "נערים ג"),
    ("ילדים טרום א", "ילדים טרום א"),
    ("טרום ילדים א", "ילדים טרום א"),
    ("ילדים טרום ב", "ילדים טרום ב"),
    ("טרום ילדים ב", "ילדים טרום ב"),
    ("ילדים טרום ג", "ילדים טרום ג"),
    ("טרום ילדים ג", "ילדים טרום ג"),
    ("ילדים א", "ילדים א"),
    ("ילדים ב", "ילדים ב"),
    ("ילדים ג", "ילדים ג"),
]


def age_bracket(name):
    """The age bracket a league name belongs to, or None if it is not a boys youth/kids league.

    Cups are excluded: they answer the fixtures fallback just like a league without
    standings, but a cup is not a division. Girls' competitions (נערות / ילדות) share
    some tokens with the boys' names and are dropped for the same reason.
    """
    if parse.classify_competition(name) != parse.LEAGUE:
        return None
    if "נערות" in name or "ילדות" in name or "נשים" in name:
        return None
    for token, bracket in _BRACKET_TOKENS:
        if token in name:
            return bracket
    return None


def load_index(path):
    """Previously discovered seasons, so a rerun only scans what it has to."""
    if not path.exists():
        return {}
    by_season = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            bracket = age_bracket(row["league_name"])
            if not bracket:
                continue
            by_season.setdefault(int(row["season_id"]), {})[int(row["league_id"])] = (
                bracket,
                row["league_name"],
            )
    return by_season


def discover_season(client, season_id):
    """Scan the id range and return {league_id: (age_group, league_name)} for youth."""
    def count_teams(league_id):
        return league_id, league_teams(client, league_id, season_id)

    active = {}
    with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
        for league_id, teams in pool.map(count_teams, SCAN_RANGE):
            if teams:
                active[league_id] = len(teams)

    def named(league_id):
        return league_id, league_name(client, league_id, season_id)

    youth = {}
    with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
        for league_id, name in pool.map(named, sorted(active)):
            bracket = age_bracket(name)
            if bracket:
                youth[league_id] = (bracket, name)

        log.info(
            "season %s: %s leagues hold teams, %s of them youth/kids",
            config.season_label(season_id), len(active), len(youth),
        )
    return youth, active


def youth_index(client, seasons, path=None, refresh=False):
    """The youth league index for each season, discovering and checkpointing as needed."""
    path = path or config.DATA_DIR / "league_index.csv"
    known = {} if refresh else load_index(path)
    missing = [s for s in seasons if s not in known]

    if missing:
        counts = {}
        for season_id in missing:
            known[season_id], counts[season_id] = discover_season(client, season_id)
        write_index(path, known, counts)

    return {season_id: known[season_id] for season_id in seasons}


def write_index(path, by_season, team_counts):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        for season_id in sorted(by_season, reverse=True):
            leagues = by_season[season_id]
            for league_id, (age_group, name) in sorted(
                leagues.items(), key=lambda kv: (kv[1][0], kv[1][1])
            ):
                writer.writerow(
                    {
                        "season_id": season_id,
                        "league_id": league_id,
                        "league_name": name,
                        "age_group": age_group,
                        "teams": (team_counts.get(season_id) or {}).get(league_id, ""),
                    }
                )
    log.info("wrote the league index -> %s", path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=sorted({**config.SEASONS, **config.HISTORY_SEASONS}, reverse=True),
        help="season ids to index (default: every configured season)",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="rescan even seasons already indexed"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    started = time.time()

    client = IFAClient()
    index = youth_index(client, args.seasons, refresh=args.refresh)
    configured = set(config.league_index())

    for season_id in sorted(index, reverse=True):
        leagues = index[season_id]
        extra = {i: n for i, (g, n) in leagues.items() if i not in configured}
        log.info(
            "%s: %s youth/kids leagues, %s beyond the configured list%s",
            config.season_label(season_id), len(leagues), len(extra),
            (": " + ", ".join(sorted(extra.values()))) if extra else "",
        )

    log.info("done in %.1f min | requests: %s", (time.time() - started) / 60, client.stats)


if __name__ == "__main__":
    main()
