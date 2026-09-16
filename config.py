"""Static configuration: target leagues, seasons, endpoints and tuning knobs."""

from pathlib import Path

BASE_URL = "https://www.football.org.il"
HOSTNAME = "www.football.org.il"

# The system resolver returns SERVFAIL for this domain, so the client resolves it
# through a public DNS server and falls back to these Cloudflare edge addresses.
FALLBACK_IPS = ["104.20.20.3", "172.66.156.146"]
PUBLIC_DNS = "8.8.8.8"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

MAX_WORKERS = 8
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5
# Per-worker pause between requests, to stay gentle on the origin.
POLITENESS_DELAY = 0.15

# season_id -> human readable label, ordered newest first.
SEASONS = {
    28: "2026/27",
    27: "2025/26",
    26: "2024/25",
}

# Seasons scraped for club history only (no goals or minutes). Goes back through the
# kids brackets.  The current dataset contains a small number of players born as
# early as 2005, so the history window reaches 2010/11 (roughly age five) rather than
# assuming everybody followed the normal current-year age ladder. Current seasons are
# included too,
# because many players in נערים still turned out for a ילדים side in 2024/25.
HISTORY_SEASONS = {
    25: "2023/24",
    24: "2022/23",
    23: "2021/22",
    22: "2020/21",
    21: "2019/20",
    20: "2018/19",
    19: "2017/18",
    18: "2016/17",
    17: "2015/16",
    16: "2014/15",
    15: "2013/14",
    14: "2012/13",
    13: "2011/12",
    12: "2010/11",
}


def season_label(season_id):
    """Label for a season id from either the stats seasons or the history seasons."""
    return SEASONS.get(season_id) or HISTORY_SEASONS[season_id]

# Youth age brackets from youngest to oldest. Playing "above age" means appearing in a
# bracket further along this ladder than where most of your birth year plays.
AGE_GROUP_LADDER = ["נערים ג", "נערים ב", "נערים א", "נוער"]

# Kids brackets, youngest to oldest. Club history includes these; the above-age flag
# does not, so aging from ילדים into נערים is not treated as "playing up".
# "ילדים טרום" is checked as a whole token so it is not filed under "ילדים א".
KIDS_BRACKETS = [
    "ילדים טרום ג",
    "ילדים טרום ב",
    "ילדים טרום א",
    "ילדים ג",
    "ילדים ב",
    "ילדים א",
]


def history_brackets():
    """Every age group that belongs on a player's club-history timeline."""
    return KIDS_BRACKETS + AGE_GROUP_LADDER

# Youth leagues grouped by age bracket, as listed in the site's main navigation.
YOUTH_LEAGUES = {
    "נוער": {
        101: "ליגת העל לנוער",
        103: "ליגה לאומית לנוער דרום",
        102: "ליגה לאומית לנוער צפון",
        105: "ליגה ארצית לנוער דרום",
        104: "ליגה ארצית לנוער צפון",
        920: "ליגת נוער יזרעאל/שרון",
        787: "ליגת נוער מפרץ",
        666: "ליגת נוער מרכז",
        110: "ליגת נוער צפון",
        115: "ליגת נוער שפלה",
        923: "ליגת אולמות לנוער",
    },
    "נערים א": {
        726: "ליגת נערים א' על",
        121: "ליגת נערים א' ארצית דרום",
        120: "ליגת נערים א' ארצית צפון",
        646: "ליגת נערים א' דן",
        755: "ליגת נערים א' יזרעאל",
        123: "ליגת נערים א' מרכז",
        122: "ליגת נערים א' צפון",
        665: "ליגת נערים א' שפלה",
        664: "ליגת נערים א' שרון",
    },
    "נערים ב": {
        773: "ליגת נערים ב' על",
        719: "ליגת נערים ב' ארצית דרום",
        720: "ליגת נערים ב' ארצית צפון",
        135: "ליגת נערים ב' דן",
        139: "ליגת נערים ב' דרום",
        706: "ליגת נערים ב' יזרעאל",
        131: "ליגת נערים ב' מפרץ",
        137: "ליגת נערים ב' מרכז",
        130: "ליגת נערים ב' צפון",
        658: "ליגת נערים ב' שפלה",
        134: "ליגת נערים ב' שרון",
    },
    "נערים ג": {
        824: "נערים ג' על",
        845: "ליגת נערים ג' ארצית דרום",
        826: "ליגת נערים ג' ארצית צפון",
        736: "ליגת נערים ג' דן",
        663: "ליגת נערים ג' דרום",
        758: "ליגת נערים ג' יזרעאל",
        759: "ליגת נערים ג' מפרץ",
        146: "ליגת נערים ג' מרכז",
        816: "ליגת נערים ג' צפון",
        707: "ליגת נערים ג' שפלה",
        144: "ליגת נערים ג' שרון",
    },
}


def league_index():
    """Flatten YOUTH_LEAGUES into league_id -> (age_group, league_name)."""
    return {
        league_id: (age_group, name)
        for age_group, leagues in YOUTH_LEAGUES.items()
        for league_id, name in leagues.items()
    }
