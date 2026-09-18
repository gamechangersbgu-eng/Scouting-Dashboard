"""Regression tests for the two causes found in the dataset-5 parity audit
(1,002 players compared, 13 mismatches across 9 players, all in
first_club_city/likely_origin_city) and fixed by the 0003 migration.

CAUSE A -- first-club tie ordering (players 160443, 206511, 227243)
---------------------------------------------------------------------
``BaseScoutingData.player()`` stably sorts a player's rows by
``(season_id, team_name)``, then ``_career_track()`` groups them into club
spells and stably sorts those by ``first_season_id``. When two rows/spells
tie on *both* keys, the stable sort falls through to whichever order the
rows arrived in. All three players below have a real tie in their earliest
season -- two (or three) team_ids sharing the byte-for-byte identical
``team_name`` -- extracted verbatim from this repository's own
``data/player_history.csv``:

* 160443, season 16: team_id 6166 and 6351, both "הפ' בני אעבלין".
* 206511, season 20: team_id 1176 and 1629, both "מכבי חיפה גולדשנפלד"
  (plus team_id 1055, "מכבי חיפה עודד", which does NOT tie on team_name).
* 227243, season 22: team_id 2117 and 2336, both "הפועל חיפה 1".

CsvScoutingData resolves the tie via pandas.read_csv's row order (i.e. the
CSV's own line order). ``player_team_season_observations`` records that same
order explicitly, per source, as ``source_row_number`` (see the 0003
migration), and ``PostgresScoutingData``'s queries now ``ORDER BY`` it.

CAUSE B -- team location mapping (players 268421, 291208, 302022)
---------------------------------------------------------------------
The importer used to deduplicate team locations by field_id
(``_load_venue_sources``/``venues``), keeping only the first
``team_locations.csv`` row seen for a shared field. Real example from this
repository's own data: field_id 48 is home to 11 different team_ids; most
geocode to "מג'ד אל כרום" but team_id 6480 -- the earliest, and only, club
for players 291208 and 302022 -- geocodes on its own team_name to a
different, real locality: "מגדל". ``dataset_team_locations`` (0003) instead
snapshots ``team_locations.csv`` verbatim, one row per team_id, with no
field-based dedup.

Both fixes are exercised together here: CsvScoutingData is built from real
CSV rows in a temp data dir, and PostgresScoutingData is built from the same
rows via faked Postgres calls (never a real database -- see
tests/test_postgres_source.py's own docstring for that convention), fed in
the exact order the real ``ORDER BY o.source_row_number`` query is expected
to produce.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from dashboard.app_core import CsvScoutingData
from dashboard.postgres_source import PostgresScoutingData
from ifa_scraper import run
from scripts.check_parity import compare_player

STAT_FIELDS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]

SEASON_STATS_COLUMNS = [
    "player_id", "player_name", "season_id", "season", "team_id", "team_name",
    "age_group", "league_name",
    *STAT_FIELDS,
]

HISTORY_COLUMNS = [
    "player_id", "player_name", "season_id", "season", "team_id", "team_name",
    "league_id", "age_group", "league_name",
    *STAT_FIELDS,
    "stats_available", "stats_source", "stats_completeness",
]

TEAM_LOCATIONS_COLUMNS = [
    "team_id", "team_name", "field_id", "field_name", "address", "city",
    "district", "lat", "lon", "geocode_query", "precision",
]

CAUSE_A_PLAYER_IDS = ["160443", "206511", "227243"]
CAUSE_B_PLAYER_IDS = ["268421", "291208", "302022"]
ALL_PLAYER_IDS = CAUSE_A_PLAYER_IDS + CAUSE_B_PLAYER_IDS

BIRTH_YEARS = {
    "160443": 2005, "206511": 2008, "227243": 2010,
    "268421": 2009, "291208": 2006, "302022": 2006,
}

# The city the CSV picks for each player's first club (verified directly
# against this repository's own data before writing the 0003 migration).
EXPECTED_FIRST_CLUB_CITY = {
    "160443": "אעבלין",
    "206511": "נהלל",
    "227243": "קרית ים",
    "268421": "בועיינה",
    "291208": "מגדל",
    "302022": "מגדל",
}

# Verbatim rows from data/player_season_stats.csv for the six players above,
# in the file's own order.
SEASON_STATS_ROWS = [
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "26", "season": "2024/25", "team_id": "1580", "team_name": "עירוני נשר", "age_group": "נערים ג", "league_name": "ליגת נערים ג' ארצית צפון", "games": "3", "goals": "0", "minutes": "55", "starts": "0", "sub_on": "3", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "26", "season": "2024/25", "team_id": "2068", "team_name": "הפועל ק\"ש", "age_group": "נערים א", "league_name": "ליגת נערים א' על", "games": "34", "goals": "0", "minutes": "3291", "starts": "34", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "4", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "26", "season": "2024/25", "team_id": "2473", "team_name": "הפ' חיפה רובי", "age_group": "נוער", "league_name": "ליגת העל לנוער", "games": "32", "goals": "2", "minutes": "3068", "starts": "32", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "10", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "268421", "player_name": "פוקרא זין אל דין", "season_id": "26", "season": "2024/25", "team_id": "3079", "team_name": "הפועל בועיינה", "age_group": "נערים ב", "league_name": "ליגת נערים ב' יזרעאל", "games": "7", "goals": "1", "minutes": "519", "starts": "6", "sub_on": "1", "sub_off": "2", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "27", "season": "2025/26", "team_id": "5308", "team_name": "הפועל עפולה", "age_group": "נוער", "league_name": "ליגה לאומית לנוער צפון", "games": "10", "goals": "0", "minutes": "702", "starts": "7", "sub_on": "3", "sub_off": "3", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "27", "season": "2025/26", "team_id": "5728", "team_name": "עירוני נשר", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "12", "goals": "0", "minutes": "247", "starts": "1", "sub_on": "11", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "291208", "player_name": "כרים האדי", "season_id": "26", "season": "2024/25", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "9", "goals": "0", "minutes": "525", "starts": "5", "sub_on": "4", "sub_off": "1", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "302022", "player_name": "מנאע שאדי", "season_id": "26", "season": "2024/25", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "3", "goals": "0", "minutes": "208", "starts": "2", "sub_on": "1", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0"},
    {"player_id": "291208", "player_name": "כרים האדי", "season_id": "27", "season": "2025/26", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "10", "goals": "1", "minutes": "802", "starts": "10", "sub_on": "0", "sub_off": "3", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "1"},
]

# Verbatim rows from data/player_history.csv, in the file's own order --
# this order is exactly what decides the Cause A ties.
HISTORY_ROWS = [
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "26", "season": "2024/25", "team_id": "2473", "team_name": "הפ' חיפה רובי", "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער", "games": "32", "goals": "2", "minutes": "3068", "starts": "32", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "10", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "25", "season": "2023/24", "team_id": "2473", "team_name": "הפ' חיפה רובי", "league_id": "101", "age_group": "נוער", "league_name": "ליגת העל לנוער", "games": "31", "goals": "0", "minutes": "2930", "starts": "31", "sub_on": "0", "sub_off": "2", "yellow_cards_league_cup": "6", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "24", "season": "2022/23", "team_id": "4087", "team_name": "מ.כ. נוה יוסף", "league_id": "102", "age_group": "נוער", "league_name": "ליגה לאומית לנוער צפון", "games": "20", "goals": "1", "minutes": "1678", "starts": "19", "sub_on": "1", "sub_off": "4", "yellow_cards_league_cup": "8", "yellow_cards_toto": "0", "red_cards": "2", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "23", "season": "2021/22", "team_id": "6540", "team_name": "הפ' בני אעבלין", "league_id": "120", "age_group": "נערים א", "league_name": "ליגת נערים א' ארצית צפון", "games": "31", "goals": "0", "minutes": "2844", "starts": "31", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "5", "yellow_cards_toto": "0", "red_cards": "2", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "22", "season": "2020/21", "team_id": "6324", "team_name": "הפ' בני אעבלין", "league_id": "720", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "16", "goals": "0", "minutes": "1325", "starts": "16", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "22", "season": "2020/21", "team_id": "6540", "team_name": "הפ' בני אעבלין", "league_id": "120", "age_group": "נערים א", "league_name": "ליגת נערים א' ארצית צפון", "games": "3", "goals": "0", "minutes": "201", "starts": "2", "sub_on": "1", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "21", "season": "2019/20", "team_id": "6324", "team_name": "הפ' בני אעבלין", "league_id": "720", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "1", "goals": "0", "minutes": "84", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "21", "season": "2019/20", "team_id": "6708", "team_name": "הפ' בני אעבלין", "league_id": "141", "age_group": "נערים ג", "league_name": "ליגת נערים ג' מפרץ", "games": "17", "goals": "0", "minutes": "1191", "starts": "17", "sub_on": "0", "sub_off": "6", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "20", "season": "2018/19", "team_id": "6708", "team_name": "הפ' בני אעבלין", "league_id": "141", "age_group": "נערים ג", "league_name": "ליגת נערים ג' מפרץ", "games": "6", "goals": "0", "minutes": "297", "starts": "5", "sub_on": "1", "sub_off": "4", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "20", "season": "2018/19", "team_id": "6165", "team_name": "הפ' בני אעבלין ''איאד''", "league_id": "152", "age_group": "ילדים א", "league_name": "ליגת ילדים א' מפרץ", "games": "19", "goals": "0", "minutes": "1080", "starts": "18", "sub_on": "1", "sub_off": "9", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "19", "season": "2017/18", "team_id": "5822", "team_name": "הפ' בני אעבלין", "league_id": "766", "age_group": "ילדים ב", "league_name": "ליגה לילדים ב' מפרץ 1", "games": "20", "goals": "0", "minutes": "1502", "starts": "20", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "18", "season": "2016/17", "team_id": "5822", "team_name": "הפ' בני אעבלין", "league_id": "161", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' מפרץ", "games": "4", "goals": "0", "minutes": "307", "starts": "4", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "18", "season": "2016/17", "team_id": "6166", "team_name": "הפ' בני אעבלין", "league_id": "738", "age_group": "ילדים ג", "league_name": "ליגה ילדים ג' מפרץ 1", "games": "11", "goals": "0", "minutes": "770", "starts": "11", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "18", "season": "2016/17", "team_id": "6351", "team_name": "הפ' בני אעבלין", "league_id": "710", "age_group": "ילדים טרום א", "league_name": "ליגה ילדים טרום א' מפרץ 1", "games": "3", "goals": "0", "minutes": "210", "starts": "3", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "17", "season": "2015/16", "team_id": "6166", "team_name": "הפ' בני אעבלין", "league_id": "649", "age_group": "ילדים ג", "league_name": "ליגת ילדים ג מפרץ", "games": "11", "goals": "0", "minutes": "770", "starts": "11", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "17", "season": "2015/16", "team_id": "6351", "team_name": "הפ' בני אעבלין", "league_id": "710", "age_group": "ילדים טרום א", "league_name": "ליגה ילדים טרום א' מפרץ 1", "games": "20", "goals": "0", "minutes": "1400", "starts": "20", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    # The determining tie: season 16, team_id 6166 (city אעבלין) listed
    # before team_id 6351 (city טמרה) -- both "הפ' בני אעבלין".
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "16", "season": "2014/15", "team_id": "6166", "team_name": "הפ' בני אעבלין", "league_id": "649", "age_group": "ילדים ג", "league_name": "ליגת ילדים ג מפרץ", "games": "1", "goals": "0", "minutes": "70", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "160443", "player_name": "ח'ליל יוחנא", "season_id": "16", "season": "2014/15", "team_id": "6351", "team_name": "הפ' בני אעבלין", "league_id": "180", "age_group": "ילדים טרום א", "league_name": "ליגת ילדים טרום א' מפרץ", "games": "9", "goals": "0", "minutes": "630", "starts": "9", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "28", "season": "2026/27", "team_id": "6516", "team_name": "מכבי בני ריינה", "league_id": "102", "age_group": "נוער", "league_name": "ליגה לאומית לנוער צפון", "games": "2", "goals": "0", "minutes": "101", "starts": "1", "sub_on": "1", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "27", "season": "2025/26", "team_id": "5308", "team_name": "הפועל עפולה", "league_id": "102", "age_group": "נוער", "league_name": "ליגה לאומית לנוער צפון", "games": "10", "goals": "0", "minutes": "702", "starts": "7", "sub_on": "3", "sub_off": "3", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "26", "season": "2024/25", "team_id": "2068", "team_name": "הפועל ק\"ש", "league_id": "726", "age_group": "נערים א", "league_name": "ליגת נערים א' על", "games": "34", "goals": "0", "minutes": "3291", "starts": "34", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "4", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "25", "season": "2023/24", "team_id": "2068", "team_name": "הפועל ק\"ש", "league_id": "726", "age_group": "נערים א", "league_name": "ליגת נערים א' על", "games": "12", "goals": "0", "minutes": "1021", "starts": "11", "sub_on": "1", "sub_off": "2", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "25", "season": "2023/24", "team_id": "2153", "team_name": "הפועל ק\"ש \"צו פיוס\"", "league_id": "773", "age_group": "נערים ב", "league_name": "ליגת נערים ב' על", "games": "26", "goals": "0", "minutes": "2161", "starts": "25", "sub_on": "1", "sub_off": "2", "yellow_cards_league_cup": "3", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "24", "season": "2022/23", "team_id": "1113", "team_name": "מכבי חיפה גולדשנפלד", "league_id": "720", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "1", "goals": "0", "minutes": "85", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "24", "season": "2022/23", "team_id": "1029", "team_name": "מכבי חיפה מקס", "league_id": "786", "age_group": "נערים ג", "league_name": "ליגת נערים ג' מרכז 1", "games": "33", "goals": "3", "minutes": "2521", "starts": "31", "sub_on": "2", "sub_off": "4", "yellow_cards_league_cup": "7", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "23", "season": "2021/22", "team_id": "4848", "team_name": "מכבי חיפה גולדשנפלד", "league_id": "154", "age_group": "ילדים א", "league_name": "ליגת ילדים א' שרון", "games": "25", "goals": "0", "minutes": "1515", "starts": "18", "sub_on": "7", "sub_off": "2", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "23", "season": "2021/22", "team_id": "1029", "team_name": "מכבי חיפה מקס", "league_id": "144", "age_group": "נערים ג", "league_name": "ליגת נערים ג' שרון", "games": "10", "goals": "0", "minutes": "797", "starts": "10", "sub_on": "0", "sub_off": "1", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "22", "season": "2020/21", "team_id": "1117", "team_name": "מכבי חיפה אפרים", "league_id": "163", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' שרון", "games": "15", "goals": "0", "minutes": "1155", "starts": "15", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "21", "season": "2019/20", "team_id": "1117", "team_name": "מכבי חיפה אפרים", "league_id": "766", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' מפרץ 1", "games": "1", "goals": "0", "minutes": "75", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "21", "season": "2019/20", "team_id": "1629", "team_name": "מכבי חיפה גולדשנפלד", "league_id": "770", "age_group": "ילדים ג", "league_name": "ילדים ג חוף", "games": "17", "goals": "0", "minutes": "1190", "starts": "17", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    # The determining tie: season 20, team_id 1176 (city נהלל) listed before
    # team_id 1629 (city חיפה) -- both "מכבי חיפה גולדשנפלד". team_id 1055
    # ("מכבי חיפה עודד", city חיפה) does not tie on team_name at all.
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "20", "season": "2018/19", "team_id": "1176", "team_name": "מכבי חיפה גולדשנפלד", "league_id": "771", "age_group": "ילדים טרום א", "league_name": "ליגה ילדים טרום א' חוף", "games": "25", "goals": "0", "minutes": "1750", "starts": "25", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "20", "season": "2018/19", "team_id": "1629", "team_name": "מכבי חיפה גולדשנפלד", "league_id": "770", "age_group": "ילדים ג", "league_name": "ליגה ילדים ג' חוף", "games": "3", "goals": "0", "minutes": "210", "starts": "3", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "206511", "player_name": "גדיר מוחמד", "season_id": "20", "season": "2018/19", "team_id": "1055", "team_name": "מכבי חיפה עודד", "league_id": "661", "age_group": "ילדים ג", "league_name": "ליגת ילדים ג' שרון", "games": "1", "goals": "0", "minutes": "70", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "28", "season": "2026/27", "team_id": "5895", "team_name": "עירוני נשר", "league_id": "937", "age_group": "נערים א", "league_name": "ליגת נערים א' לאומית צפון", "games": "0", "goals": "0", "minutes": "0", "starts": "0", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "27", "season": "2025/26", "team_id": "5728", "team_name": "עירוני נשר \"צו פיוס\"", "league_id": "720", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "12", "goals": "0", "minutes": "247", "starts": "1", "sub_on": "11", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "26", "season": "2024/25", "team_id": "1580", "team_name": "עירוני נשר", "league_id": "826", "age_group": "נערים ג", "league_name": "ליגת נערים ג' ארצית צפון", "games": "3", "goals": "0", "minutes": "55", "starts": "0", "sub_on": "3", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "26", "season": "2024/25", "team_id": "5733", "team_name": "עירוני נשר", "league_id": "154", "age_group": "ילדים א", "league_name": "ליגת ילדים א' שרון", "games": "13", "goals": "0", "minutes": "423", "starts": "3", "sub_on": "10", "sub_off": "3", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "25", "season": "2023/24", "team_id": "5733", "team_name": "עירוני נשר", "league_id": "827", "age_group": "ילדים א", "league_name": "ליגת ילדים א' דרג 1", "games": "18", "goals": "1", "minutes": "936", "starts": "13", "sub_on": "5", "sub_off": "9", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "24", "season": "2022/23", "team_id": "5734", "team_name": "עירוני נשר \"צו פיוס\"", "league_id": "804", "age_group": "ילדים ב", "league_name": "ליגת ילדים ב' חוף", "games": "22", "goals": "0", "minutes": "1632", "starts": "22", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "23", "season": "2021/22", "team_id": "2336", "team_name": "הפועל חיפה 1", "league_id": "661", "age_group": "ילדים ג", "league_name": "ליגת ילדים ג' שרון", "games": "13", "goals": "0", "minutes": "959", "starts": "13", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    # The determining tie: season 22, team_id 2117 (city קרית ים) listed
    # before team_id 2336 (city חיפה) -- both "הפועל חיפה 1".
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "22", "season": "2020/21", "team_id": "2117", "team_name": "הפועל חיפה 1", "league_id": "180", "age_group": "ילדים טרום א", "league_name": "ליגת ילדים טרום א' מפרץ", "games": "13", "goals": "0", "minutes": "929", "starts": "13", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "227243", "player_name": "טאהא ג'מיל", "season_id": "22", "season": "2020/21", "team_id": "2336", "team_name": "הפועל חיפה 1", "league_id": "770", "age_group": "ילדים ג", "league_name": "ילדים ג חוף", "games": "1", "goals": "0", "minutes": "73", "starts": "1", "sub_on": "0", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "268421", "player_name": "פוקרא זין אל דין", "season_id": "26", "season": "2024/25", "team_id": "3079", "team_name": "הפועל בועיינה", "league_id": "706", "age_group": "נערים ב", "league_name": "ליגת נערים ב' יזרעאל", "games": "7", "goals": "1", "minutes": "519", "starts": "6", "sub_on": "1", "sub_off": "2", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "268421", "player_name": "פוקרא זין אל דין", "season_id": "25", "season": "2023/24", "team_id": "3079", "team_name": "הפועל בועיינה", "league_id": "720", "age_group": "נערים ב", "league_name": "ליגת נערים ב' ארצית צפון", "games": "13", "goals": "1", "minutes": "676", "starts": "8", "sub_on": "5", "sub_off": "2", "yellow_cards_league_cup": "1", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "268421", "player_name": "פוקרא זין אל דין", "season_id": "25", "season": "2023/24", "team_id": "3084", "team_name": "הפועל בועיינה", "league_id": "816", "age_group": "נערים ג", "league_name": "ליגת נערים ג' צפון", "games": "12", "goals": "2", "minutes": "848", "starts": "9", "sub_on": "3", "sub_off": "1", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "291208", "player_name": "כרים האדי", "season_id": "27", "season": "2025/26", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "league_id": "110", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "10", "goals": "1", "minutes": "802", "starts": "10", "sub_on": "0", "sub_off": "3", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "1", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "291208", "player_name": "כרים האדי", "season_id": "26", "season": "2024/25", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "league_id": "110", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "9", "goals": "0", "minutes": "525", "starts": "5", "sub_on": "4", "sub_off": "1", "yellow_cards_league_cup": "2", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
    {"player_id": "302022", "player_name": "מנאע שאדי", "season_id": "26", "season": "2024/25", "team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "league_id": "110", "age_group": "נוער", "league_name": "ליגת נוער צפון", "games": "3", "goals": "0", "minutes": "208", "starts": "2", "sub_on": "1", "sub_off": "0", "yellow_cards_league_cup": "0", "yellow_cards_toto": "0", "red_cards": "0", "stats_available": "True", "stats_source": "team_player_statistics", "stats_completeness": "full"},
]

# Verbatim rows from data/team_locations.csv for every team_id above.
# field_id 48 (team 6480) and field_id 1311 (teams 3079/3084) are the real
# multi-team-per-field cases this migration exists to stop collapsing.
TEAM_LOCATIONS = [
    {"team_id": "1055", "team_name": "מכבי חיפה עודד", "field_id": "636", "field_name": "חיפה קצף סינטטי", "address": "צביה ויצחק חיפה", "city": "חיפה", "district": "צפון", "lat": "32.7941142", "lon": "34.9684183", "geocode_query": "צביה ויצחק חיפה", "precision": "street"},
    {"team_id": "1176", "team_name": "מכבי חיפה גולדשנפלד", "field_id": "250", "field_name": "נהלל", "address": "נהלל", "city": "נהלל", "district": "צפון", "lat": "32.6900631", "lon": "35.1968485", "geocode_query": "נהלל", "precision": "street"},
    {"team_id": "1629", "team_name": "מכבי חיפה גולדשנפלד", "field_id": "636", "field_name": "חיפה קצף סינטטי", "address": "צביה ויצחק חיפה", "city": "חיפה", "district": "צפון", "lat": "32.7941142", "lon": "34.9684183", "geocode_query": "צביה ויצחק חיפה", "precision": "street"},
    {"team_id": "2117", "team_name": "הפועל חיפה 1", "field_id": "1269", "field_name": "קרית ים סינטטי", "address": "פנחס לבון 1 קרית ים", "city": "קרית ים", "district": "צפון", "lat": "32.8553846", "lon": "35.0765524", "geocode_query": "פנחס לבון 1 קרית ים", "precision": "street"},
    {"team_id": "2336", "team_name": "הפועל חיפה 1", "field_id": "1239", "field_name": "קרית חיים בית נגלר מערבי (אתוס לשעבר)", "address": "אברהם דנינו 6 חיפה", "city": "חיפה", "district": "צפון", "lat": "32.8181708", "lon": "35.066066", "geocode_query": "אברהם דנינו 6 חיפה", "precision": "street"},
    {"team_id": "6166", "team_name": "הפ' בני אעבלין", "field_id": "236", "field_name": "אעבלין", "address": "אעבלין", "city": "אעבלין", "district": "צפון", "lat": "32.8209166", "lon": "35.19133", "geocode_query": "אעבלין", "precision": "street"},
    {"team_id": "6351", "team_name": "הפ' בני אעבלין", "field_id": "254", "field_name": "טמרה", "address": "טמרה", "city": "טמרה", "district": "צפון", "lat": "32.85301", "lon": "35.1987", "geocode_query": "טמרה", "precision": "locality"},
    # field_id 48: 6480 geocodes on its own team_name to a different,
    # distinct locality than the other teams that share the same field
    # (not all included here -- only 6480 is relevant to these six players).
    {"team_id": "6480", "team_name": "מ.כ. מג'דל כרום שאגור", "field_id": "48", "field_name": "מג'ד אל כרום", "address": "מג'ד אל כרום", "city": "מגדל", "district": "צפון", "lat": "32.83931", "lon": "35.50206", "geocode_query": "מגדל", "precision": "locality"},
    # field_id 1311: 3079/3084 ("הפועל בועיינה") share it with "מכבי
    # נוג'ידאת אחמד" (not included -- irrelevant to these six players), but
    # both resolve, independently, to their own real city "בועיינה".
    {"team_id": "3079", "team_name": "הפועל בועיינה", "field_id": "1311", "field_name": "בועיינה נוג'ידאת מזרחי חדש", "address": "אלזיתון 1 בועיינה נוג'יידאת", "city": "בועיינה", "district": "צפון", "lat": "32.8056259", "lon": "35.3646736", "geocode_query": "בועיינה", "precision": "fallback"},
    {"team_id": "3084", "team_name": "הפועל בועיינה", "field_id": "1311", "field_name": "בועיינה נוג'ידאת מזרחי חדש", "address": "אלזיתון 1 בועיינה נוג'יידאת", "city": "בועיינה", "district": "צפון", "lat": "32.8056259", "lon": "35.3646736", "geocode_query": "בועיינה", "precision": "fallback"},
]


def _write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class _RoutingCursor:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self._rows = []

    def execute(self, sql, params=None):
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                self._rows = rows
                return
        raise AssertionError(f"no fake rows registered for query: {sql[:80]}")

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker

    def cursor(self):
        return _RoutingCursor(self.rows_by_marker)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class RowOrderAndLocationParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

        _write_csv(self.data_dir / "player_season_stats.csv", SEASON_STATS_ROWS, SEASON_STATS_COLUMNS)
        _write_csv(self.data_dir / "player_history.csv", HISTORY_ROWS, HISTORY_COLUMNS)
        _write_csv(self.data_dir / "team_locations.csv", TEAM_LOCATIONS, TEAM_LOCATIONS_COLUMNS)
        _write_csv(
            self.data_dir / "player_details.csv",
            [{"player_id": pid, "birth_year": str(year), "image_url": ""} for pid, year in BIRTH_YEARS.items()],
            ["player_id", "birth_year", "image_url"],
        )

        details = {pid: {"birth_year": year} for pid, year in BIRTH_YEARS.items()}
        season_rows_for_aggregate = [
            {**row, "season_id": int(row["season_id"]), **{f: int(row[f]) for f in STAT_FIELDS}}
            for row in SEASON_STATS_ROWS
        ]
        players = run.aggregate_players(season_rows_for_aggregate, splits={}, details=details)
        run.write_csv(self.data_dir / "players_youth.csv", run.PLAYER_COLUMNS, players)

        self.csv_data = CsvScoutingData(self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def _frame(self, rows, has_league_id):
        out = []
        for row in rows:
            entry = {
                "player_id": row["player_id"], "player_name": row["player_name"],
                "season_id": int(row["season_id"]), "season": row["season"],
                "team_id": row["team_id"], "team_name": row["team_name"],
                "league_id": row.get("league_id", "") if has_league_id else "",
                "age_group": row["age_group"], "league_name": row["league_name"],
                **{field: int(row[field]) for field in STAT_FIELDS},
            }
            if has_league_id:
                entry["stats_available"] = row["stats_available"] == "True"
                entry["stats_source"] = row["stats_source"]
                entry["stats_completeness"] = row["stats_completeness"]
            else:
                entry["stats_available"] = None
                entry["stats_source"] = None
                entry["stats_completeness"] = None
            out.append(entry)
        return pd.DataFrame(out)

    def _make_pg_data(self):
        # Rows are fed in exactly SEASON_STATS_ROWS/HISTORY_ROWS's own order --
        # i.e. what "ORDER BY o.source_row_number" is expected to reproduce
        # (see the 0003 migration and the smoke checks in
        # tests/test_postgres_source.py that the real SQL text has that
        # clause). This is what actually exercises the tie-break fix: if this
        # order were wrong, the assertions below would fail the same way the
        # pre-0003 dataset-5 parity audit did.
        season_frame = self._frame(SEASON_STATS_ROWS, has_league_id=False)
        history_frame = self._frame(HISTORY_ROWS, has_league_id=True)

        season_catalog_rows = [
            (
                row["player_id"], row["player_name"], int(row["season_id"]), row["season"],
                row["team_id"], row["team_name"], row["age_group"], row["league_name"],
                *[int(row[field]) for field in STAT_FIELDS],
            )
            for row in SEASON_STATS_ROWS
        ]
        location_rows = [
            (
                row["team_id"], row["field_name"], row["city"], row["address"],
                float(row["lat"]), float(row["lon"]), row["precision"],
            )
            for row in TEAM_LOCATIONS
        ]
        connection = _FakeConnection(
            {
                "FROM player_team_season_observations o": season_catalog_rows,
                "birth_year, image_url FROM players": [
                    (pid, year, "") for pid, year in BIRTH_YEARS.items()
                ],
                "SELECT player_id, birth_year FROM players": [
                    (pid, year) for pid, year in BIRTH_YEARS.items()
                ],
                "FROM dataset_team_locations": location_rows,
            }
        )
        with mock.patch(
            "dashboard.postgres_source.db.connect", return_value=connection
        ), mock.patch(
            "dashboard.postgres_source.db.current_dataset_id", return_value=1
        ), mock.patch.object(
            PostgresScoutingData,
            "_read_season_rows",
            side_effect=lambda source_file: (
                season_frame if source_file == "player_season_stats" else history_frame
            ),
        ):
            return PostgresScoutingData(database_url="postgresql://fake/fake")

    def test_csv_side_matches_the_audited_expected_cities(self):
        # Pins down the CSV's own behavior against the values confirmed
        # during the audit, so a future change to the tie-break/location
        # logic that happens to break both sides identically still shows up.
        for player_id in ALL_PLAYER_IDS:
            player = self.csv_data.player(player_id)
            self.assertEqual(
                player["first_club_city"], EXPECTED_FIRST_CLUB_CITY[player_id], player_id
            )

    def test_known_players_retain_parity_after_0003_fixes(self):
        pg_data = self._make_pg_data()
        mismatches = []
        for player_id in ALL_PLAYER_IDS:
            compare_player(self.csv_data, pg_data, player_id, mismatches)
        self.assertEqual(mismatches, [])

    def test_postgres_side_matches_the_audited_expected_cities(self):
        pg_data = self._make_pg_data()
        for player_id in ALL_PLAYER_IDS:
            player = pg_data.player(player_id)
            self.assertEqual(
                player["first_club_city"], EXPECTED_FIRST_CLUB_CITY[player_id], player_id
            )

    def test_cause_a_tie_break_uses_row_order_not_alphabetical_team_name(self):
        # Explicit regression for the three named tie-ordering players: each
        # tied pair/triple shares an identical team_name, so an alphabetical
        # secondary sort could not disambiguate them either -- only row order
        # can. Reversing SEASON_STATS_ROWS/HISTORY_ROWS's order for the tied
        # rows would flip these results; this pins the CSV's actual choice.
        pg_data = self._make_pg_data()
        for player_id in CAUSE_A_PLAYER_IDS:
            csv_city = self.csv_data.player(player_id)["first_club_city"]
            pg_city = pg_data.player(player_id)["first_club_city"]
            self.assertEqual(csv_city, pg_city, player_id)
            self.assertEqual(csv_city, EXPECTED_FIRST_CLUB_CITY[player_id], player_id)

    def test_cause_b_location_uses_per_team_id_not_field_id_dedup(self):
        pg_data = self._make_pg_data()
        for player_id in CAUSE_B_PLAYER_IDS:
            csv_city = self.csv_data.player(player_id)["first_club_city"]
            pg_city = pg_data.player(player_id)["first_club_city"]
            self.assertEqual(csv_city, pg_city, player_id)
            self.assertEqual(csv_city, EXPECTED_FIRST_CLUB_CITY[player_id], player_id)
        # And directly: team_id 6480 must keep its own city, distinct from
        # what field_id 48's *other* occupants would give it (the old,
        # field_id-deduped venues path could pick "מג'ד אל כרום" instead).
        self.assertEqual(pg_data.locations["6480"]["city"], "מגדל")
        self.assertNotEqual(pg_data.locations["6480"]["city"], "מג'ד אל כרום")


if __name__ == "__main__":
    unittest.main()
