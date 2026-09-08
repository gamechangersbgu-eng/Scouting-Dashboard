"""Resolve every youth team to a map coordinate.

The association does not publish a club location, but it does publish each club's home
grounds and a directory of grounds with street addresses. Chaining the two gives a
location per team, which is then geocoded. Run after the main scrape:

    python -m ifa_scraper.venues
"""

import argparse
import csv
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import config, parse
from .client import IFAClient
from .gazetteer import Gazetteer
from .geocode import Geocoder

log = logging.getLogger(__name__)

TEAM_FIELD_COLUMNS = ["team_id", "team_name", "field_id", "field_name", "num_fields"]
LOCATION_COLUMNS = [
    "team_id",
    "team_name",
    "field_id",
    "field_name",
    "address",
    "city",
    "lat",
    "lon",
    "geocode_query",
    "precision",
]

# How a team's coordinates were arrived at, best first.
PRECISION_STREET = "street"
PRECISION_LOCALITY = "locality"
PRECISION_FALLBACK = "fallback"
PRECISION_NONE = "unresolved"

# Club-name furniture that carries no geographic information. Stripping it leaves the
# place name, which is what a fallback geocode needs.
CLUB_PREFIXES = [
    "מכבי", "הפועל", "הפ'", "בית\"ר", "בני", "עירוני", "מ.ס.", "מ.ס", "א.ס.", "א.ס",
    "ש.", "צעירי", "אתלטיקו", "ספורטינג", "נערי", "מ.כ.",
]

# Abbreviations the association uses inside team names, and their full forms.
NAME_ABBREVIATIONS = {
    "ת\"א": "תל אביב",
    "פ\"ת": "פתח תקווה",
    "י-ם": "ירושלים",
    "י\"ם": "ירושלים",
    "ר\"ג": "רמת גן",
    "ב\"ש": "באר שבע",
    "ק.": "קרית",
    "ע.ק.": "קרית",
    "ע.": "",
    "כ\"ס": "כפר סבא",
    "ר\"ל": "ראשון לציון",
    "ר\"ע": "רמת עמל",
    "נ\"ע": "נשר עמל",
}

_QUOTED_NICKNAME_RE = re.compile(r'"[^"]*"|״[^״]*״|\'[^\']*\'')
_MULTISPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")
_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")

# Israeli locality names run to about five words ("כוכב יאיר צור יגאל").
MAX_PLACE_WORDS = 5


def place_hint(team_name):
    """Best-effort place name from a team name, for teams whose ground is unknown.

    Team names embed the town ("מכבי ע.ק. אתא"), often abbreviated and sometimes with a
    sponsor's name attached in quotes. This strips the club furniture and expands
    abbreviations; it is a fallback only, so being approximate is acceptable.

    The order of the three steps matters. Several club words and abbreviations spell
    their gershayim as an ASCII quote (בית"ר, ר"ג), so stripping quoted nicknames first
    would match from that quote and swallow the town name with it.
    """
    words = [w for w in (team_name or "").split() if w not in CLUB_PREFIXES]
    name = " ".join(words)
    for abbreviation, expansion in NAME_ABBREVIATIONS.items():
        name = name.replace(abbreviation, f" {expansion} ")
    name = _QUOTED_NICKNAME_RE.sub(" ", name)
    return _MULTISPACE_RE.sub(" ", name).strip()


def word_groups(text, from_start=False, max_words=MAX_PLACE_WORDS):
    """Trailing (or leading) runs of words from a string, longest first.

    Addresses end with their locality and ground names begin with it, so these are the
    candidate place names to try when the gazetteer has no entry to anchor on.
    """
    words = [w for w in _DIGITS_RE.sub(" ", text or "").split() if w]
    groups = []
    for size in range(min(max_words, len(words)), 0, -1):
        groups.append(" ".join(words[:size] if from_start else words[-size:]))
    return groups


def teams_to_locate(stats_path):
    """Every team in the scraped stats, with the newest season it appears in.

    The team page is season-scoped, so each team is fetched for a season it actually
    played in.
    """
    stats = pd.read_csv(stats_path, encoding="utf-8-sig", dtype={"team_id": str})
    latest = stats.groupby(["team_id", "team_name"], as_index=False)["season_id"].max()
    return [
        (row.team_id, row.team_name, int(row.season_id))
        for row in latest.itertuples(index=False)
    ]


def load_team_field_checkpoint(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(TEAM_FIELD_COLUMNS).issubset(reader.fieldnames or []):
            return {}
        return {row["team_id"]: row for row in reader}


def collect_team_fields(client, teams, checkpoint_path):
    """Map each team to its home ground, checkpointing as it goes.

    Team pages are ~190KB and only the grounds block is needed, so the extracted rows
    are appended to a checkpoint instead of caching the HTML.
    """
    known = load_team_field_checkpoint(checkpoint_path)
    pending = [t for t in teams if t[0] not in known]
    log.info("team grounds: %s to fetch (%s already known)", len(pending), len(known))
    if not pending:
        return known

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    def fetch(team):
        team_id, team_name, season_id = team
        page = client.team_page(team_id, season_id)
        if page is None:
            return None
        fields = parse.parse_team_fields(page)
        field_id, field_name = fields[0] if fields else ("", "")
        return {
            "team_id": team_id,
            "team_name": team_name,
            "field_id": field_id,
            "field_name": field_name,
            "num_fields": len(fields),
        }

    mode = "a" if known else "w"
    with checkpoint_path.open(mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TEAM_FIELD_COLUMNS)
        if not known:
            writer.writeheader()
        with ThreadPoolExecutor(config.MAX_WORKERS) as pool:
            for index, row in enumerate(pool.map(fetch, pending), 1):
                # Leave failures unrecorded so a later run retries them.
                if row is not None:
                    known[row["team_id"]] = row
                    writer.writerow(row)
                if index % 50 == 0 or index == len(pending):
                    handle.flush()
                    log.info("team grounds: %s/%s", index, len(pending))
    return known


def locate_team(gazetteer, geocoder, team_name, mapping, field):
    """Resolve one team to coordinates, preferring the most reliable source available.

    The gazetteer fixes the locality first and everything else is measured against it,
    so a bad street match can only ever be discarded, never relocate the club.
    """
    address = field.get("address", "")
    field_name = mapping.get("field_name", "")
    hint = place_hint(team_name)

    anchor = (
        gazetteer.match_suffix(address)
        or gazetteer.match_prefix(field_name)
        or gazetteer.match_prefix(hint)
    )
    if anchor:
        street = geocoder.refine_to_street(address, anchor)
        if street:
            return {
                "lat": street["lat"],
                "lon": street["lon"],
                # Prefer the gazetteer's Hebrew name over Nominatim's, which may come
                # back in Arabic or as a regional council.
                "city": anchor["name"],
                "query": street["query"],
                "precision": PRECISION_STREET,
            }
        return {
            "lat": anchor["lat"],
            "lon": anchor["lon"],
            "city": anchor["name"],
            "query": anchor["query"],
            "precision": PRECISION_LOCALITY,
        }

    # Nothing in the gazetteer matched, so there is no anchor to validate against and
    # a free-text lookup has to stand on its own. Whole strings rarely resolve ("אלמדרסה
    # כאוכב אבו אל היגא" is a school plus a village), so word runs are tried too.
    probes = [
        address,
        *word_groups(address),
        field_name,
        *word_groups(field_name, from_start=True),
        hint,
        *word_groups(hint, from_start=True),
    ]
    settlement = geocoder.find_settlement(*dict.fromkeys(p for p in probes if p))
    if settlement:
        city = settlement.get("city") or ""
        # Small places resolve to their regional council, and Arab localities to their
        # Arabic name. Neither reads well in a Hebrew table, and the query that found
        # the place is already the association's own Hebrew spelling.
        if not city or city.startswith("מועצה") or not _HEBREW_RE.search(city):
            city = settlement["query"]
        return {
            "lat": settlement["lat"],
            "lon": settlement["lon"],
            "city": city,
            "query": settlement["query"],
            "precision": PRECISION_FALLBACK,
        }
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-grounds",
        action="store_true",
        help="re-fetch the team-to-ground mapping instead of using the checkpoint",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    started = time.time()

    client = IFAClient()
    teams = teams_to_locate(config.DATA_DIR / "player_season_stats.csv")
    log.info("%s teams to locate", len(teams))

    checkpoint = config.DATA_DIR / "team_fields.csv"
    if args.refresh_grounds:
        checkpoint.unlink(missing_ok=True)
    team_fields = collect_team_fields(client, teams, checkpoint)

    directory = parse.parse_fields_directory(client.fields_directory())
    addresses = {field["field_id"]: field for field in directory}
    log.info("grounds directory: %s entries", len(addresses))

    gazetteer = Gazetteer()
    geocoder = Geocoder()
    rows = []
    for team_id, team_name, _ in teams:
        mapping = team_fields.get(team_id) or {}
        field = addresses.get(mapping.get("field_id")) or {}
        located = locate_team(gazetteer, geocoder, team_name, mapping, field)
        rows.append(
            {
                "team_id": team_id,
                "team_name": team_name,
                "field_id": mapping.get("field_id", ""),
                "field_name": mapping.get("field_name", ""),
                "address": field.get("address", ""),
                "city": (located or {}).get("city") or place_hint(team_name),
                "lat": located["lat"] if located else "",
                "lon": located["lon"] if located else "",
                "geocode_query": located["query"] if located else "",
                "precision": located["precision"] if located else PRECISION_NONE,
            }
        )
        if len(rows) % 50 == 0:
            geocoder.save()
            log.info("geocoded %s/%s teams | %s", len(rows), len(teams), geocoder.stats)

    geocoder.save()

    out_path = config.DATA_DIR / "team_locations.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOCATION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    resolved = sum(1 for row in rows if row["lat"] != "")
    by_precision = Counter(row["precision"] for row in rows)
    log.info(
        "done in %.1f min | %s/%s teams located -> %s",
        (time.time() - started) / 60,
        resolved,
        len(rows),
        out_path,
    )
    log.info("precision: %s | geocoder: %s", dict(by_precision), geocoder.stats)
    unresolved = [row["team_name"] for row in rows if row["lat"] == ""]
    for name in unresolved:
        log.warning("unresolved: %s", name)


if __name__ == "__main__":
    main()
