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
POLITENESS_DELAY = 0.15

# Default scrape: current season only. Historical seasons can be requested explicitly
# with: python -m ifa_scraper.run --seasons 27 26
SEASONS = {
    28: "2026/27",
}

AGE_GROUP_LADDER = ["נערים ג", "נערים ב", "נערים א", "נוער"]

YOUTH_LEAGUES = {
    "נוער": {
        101: "ליגת העל לנוער", 103: "ליגה לאומית לנוער דרום", 102: "ליגה לאומית לנוער צפון",
        105: "ליגה ארצית לנוער דרום", 104: "ליגה ארצית לנוער צפון", 920: "ליגת נוער יזרעאל/שרון",
        787: "ליגת נוער מפרץ", 666: "ליגת נוער מרכז", 110: "ליגת נוער צפון", 115: "ליגת נוער שפלה", 923: "ליגת אולמות לנוער",
    },
    "נערים א": {
        726: "ליגת נערים א' על", 121: "ליגת נערים א' ארצית דרום", 120: "ליגת נערים א' ארצית צפון",
        646: "ליגת נערים א' דן", 755: "ליגת נערים א' יזרעאל", 123: "ליגת נערים א' מרכז",
        122: "ליגת נערים א' צפון", 665: "ליגת נערים א' שפלה", 664: "ליגת נערים א' שרון",
    },
    "נערים ב": {
        773: "ליגת נערים ב' על", 719: "ליגת נערים ב' ארצית דרום", 720: "ליגת נערים ב' ארצית צפון",
        135: "ליגת נערים ב' דן", 139: "ליגת נערים ב' דרום", 706: "ליגת נערים ב' יזרעאל",
        131: "ליגת נערים ב' מפרץ", 137: "ליגת נערים ב' מרכז", 130: "ליגת נערים ב' צפון",
        658: "ליגת נערים ב' שפלה", 134: "ליגת נערים ב' שרון",
    },
    "נערים ג": {
        824: "נערים ג' על", 845: "ליגת נערים ג' ארצית דרום", 826: "ליגת נערים ג' ארצית צפון",
        736: "ליגת נערים ג' דן", 663: "ליגת נערים ג' דרום", 758: "ליגת נערים ג' יזרעאל",
        759: "ליגת נערים ג' מפרץ", 146: "ליגת נערים ג' מרכז", 816: "ליגת נערים ג' צפון",
        707: "ליגת נערים ג' שפלה", 144: "ליגת נערים ג' שרון",
    },
}


def league_index():
    return {
        league_id: (age_group, name)
        for age_group, leagues in YOUTH_LEAGUES.items()
        for league_id, name in leagues.items()
    }
