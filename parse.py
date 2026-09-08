"""Parsers for the three Components.asmx endpoints used by the scraper.

Each endpoint wraps an HTML fragment inside either a JSON or an XML envelope
depending on the request headers, so every parser unwraps first.
"""

import html
import json
import re

LEAGUE = "league"
CUP = "cup"
TOTO = "toto"

_TEAM_ROW_RE = re.compile(
    r"href='/team-details/\?season_id=\d+&team_id=(\d+)'>(.*?)</a>", re.S
)
_TEAM_NAME_RE = re.compile(
    r"<div class='table_col align_content team_name'>.*?"
    r"<span class='sr-only'>קבוצה</span>([^<]*)</div>",
    re.S,
)
_SQUAD_ROW_RE = re.compile(
    r"<a class='table_row link_url'[^>]*?player_id=(\d+)[^>]*>(.*?)</a>", re.S
)
_CELL_RE = re.compile(
    r"<div class='table_col[^']*'>\s*<span class='sr-only'>([^<]*)</span>([^<]*)</div>"
)
_GAME_ROW_SPLIT_RE = re.compile(r"(?=<a class='table_row link_url player-games-tbl-row')")
_GAME_ID_RE = re.compile(r"game_id=(\d+)")
_DATE_RE = re.compile(r">תאריך</span>([^<]*)")
_COMPETITION_RE = re.compile(r">מסגרת</span>([^<]*)")
_GOALS_CELL_RE = re.compile(r">שערים:</span>(.*?)</div>", re.S)
_CARDS_CELL_RE = re.compile(r"player-cards-col'>(.*?)</div>", re.S)
_CHANGE_UP_RE = re.compile(r"change-up'\s*>.*?</span>\s*(\d+)", re.S)
_CHANGE_DOWN_RE = re.compile(r"change-down'\s*>.*?</span>\s*(\d+)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# The player card renders the date of birth as "<strong>תאריך לידה: </strong>11/2010".
_BIRTH_DATE_RE = re.compile(r"תאריך לידה:\s*</strong>\s*(?:(\d{1,2})/)?(\d{4})")
_PLAYER_IMAGE_RE = re.compile(
    r'<figure class="new-player-card_img-container">\s*<img[^>]*src="([^"]+)"', re.S
)
# The team page lists the club's home grounds in its own sidebar block.
_TEAM_FIELDS_BLOCK_RE = re.compile(
    r'<div class="fields-list[^"]*">\s*<h2>רשימת מגרשים</h2>(.*?)</div>', re.S
)
_FIELD_LINK_RE = re.compile(r'field_id=(\d+)"[^>]*>([^<]*)</a>')
_FIELD_ITEM_SPLIT_RE = re.compile(r"(?=<article class='field-item )")
_FIELD_ITEM_HEAD_RE = re.compile(r"field-item id(\d+)(?:\s+region(\d+))?")
_FIELD_ITEM_NAME_RE = re.compile(r"<h2>(.*?)</h2>", re.S)
_FIELD_ITEM_ADDRESS_RE = re.compile(r"<span class='address'>כתובת:\s*(.*?)</span>", re.S)
# Grounds withdrawn from use are published with a "(סגור)" prefix on the name.
_FIELD_CLOSED_RE = re.compile(r"^\s*\(סגור\)\s*")

# Hebrew column labels in the squad statistics table, mapped to output field names.
SQUAD_FIELDS = {
    "שם השחקן": "player_name",
    "מס. משחקים": "games",
    "שערים": "goals",
    "כ. צהובים ליגה/גביע": "yellow_cards_league_cup",
    "כ. צהובים טוטו": "yellow_cards_toto",
    "כ. אדומים": "red_cards",
    "הרכב פותח": "starts",
    "נכנס כמחליף": "sub_on",
    "הוחלף": "sub_off",
    "דקות משחק": "minutes",
}

_NUMERIC_SQUAD_FIELDS = [
    "games",
    "goals",
    "yellow_cards_league_cup",
    "yellow_cards_toto",
    "red_cards",
    "starts",
    "sub_on",
    "sub_off",
    "minutes",
]


def unwrap_response(text):
    """Pull the inner HTML fragment out of a JSON or XML service envelope."""
    if not text:
        return ""
    stripped = text.lstrip()

    if stripped.startswith("{"):
        try:
            payload = json.loads(text).get("d")
        except (ValueError, AttributeError):
            return ""
        if isinstance(payload, dict):
            payload = payload.get("HtmlData")
        return payload or ""

    match = re.search(r"<(?:HtmlData|string)[^>]*>(.*)</(?:HtmlData|string)>", text, re.S)
    if match:
        return html.unescape(match.group(1))
    # A self-closing <string /> means the service returned no rows.
    return ""


def _to_int(value):
    digits = re.sub(r"[^\d-]", "", value or "")
    try:
        return int(digits)
    except ValueError:
        return 0


def classify_competition(name):
    """Bucket a competition name (מסגרת) into league, cup or Toto Cup."""
    if not name:
        return LEAGUE
    if "טוטו" in name:
        return TOTO
    if "גביע" in name:
        return CUP
    return LEAGUE


def parse_league_teams(text):
    """Return {team_id: team_name} from a League_AllTables response."""
    fragment = unwrap_response(text)
    # The league table nests one more layer of escaping inside the envelope.
    fragment = html.unescape(fragment)
    teams = {}
    for team_id, body in _TEAM_ROW_RE.findall(fragment):
        name_match = _TEAM_NAME_RE.search(body)
        name = name_match.group(1).strip() if name_match else ""
        # Keep the first non-empty name seen for a given team.
        if team_id not in teams or (name and not teams[team_id]):
            teams[team_id] = name
    return teams


def parse_squad_stats(text):
    """Return a list of per-player season stat dicts from GetTeamPlayersStatisticsList."""
    fragment = unwrap_response(text)
    if not fragment:
        return []

    players = []
    for player_id, body in _SQUAD_ROW_RE.findall(fragment):
        row = {"player_id": player_id, "player_name": ""}
        for label, value in _CELL_RE.findall(body):
            field = SQUAD_FIELDS.get(label.strip())
            if not field:
                continue
            row[field] = value.strip() if field == "player_name" else _to_int(value)
        for field in _NUMERIC_SQUAD_FIELDS:
            row.setdefault(field, 0)
        players.append(row)
    return players


def parse_player_games(text):
    """Return per-game rows from GetPlayerGames, each tagged with its competition."""
    fragment = unwrap_response(text)
    if not fragment:
        return []

    games = []
    for chunk in _GAME_ROW_SPLIT_RE.split(fragment):
        if "player-games-tbl-row" not in chunk:
            continue

        competition = _COMPETITION_RE.search(chunk)
        competition = competition.group(1).strip() if competition else ""

        goals = 0
        goals_cell = _GOALS_CELL_RE.search(chunk)
        if goals_cell:
            text_value = _TAG_RE.sub("", goals_cell.group(1)).strip()
            if text_value.isdigit():
                goals = int(text_value)
            else:
                # Some rows render goals as one football icon per goal.
                goals = len(re.findall(r"football\.svg", goals_cell.group(1)))

        cards_cell = _CARDS_CELL_RE.search(chunk)
        cards = cards_cell.group(1) if cards_cell else ""

        game_id = _GAME_ID_RE.search(chunk)
        date = _DATE_RE.search(chunk)
        came_on = _CHANGE_UP_RE.search(chunk)
        came_off = _CHANGE_DOWN_RE.search(chunk)

        games.append(
            {
                "game_id": game_id.group(1) if game_id else "",
                "date": date.group(1).strip() if date else "",
                "competition": competition,
                "competition_type": classify_competition(competition),
                "goals": goals,
                "yellow_cards": len(re.findall(r"card-yellow", cards)),
                "red_cards": len(re.findall(r"card-red", cards)),
                "came_on_minute": _to_int(came_on.group(1)) if came_on else None,
                "came_off_minute": _to_int(came_off.group(1)) if came_off else None,
            }
        )
    return games


def parse_player_birth(page):
    """Return (birth_year, birth_month) from a player page; month may be None.

    The association publishes month and year only, never a day.
    """
    if not page:
        return None, None
    match = _BIRTH_DATE_RE.search(page)
    if not match:
        return None, None
    month, year = match.group(1), match.group(2)
    return int(year), int(month) if month else None


def parse_player_image(page):
    """Return the player's photo URL from the player card, or None if absent.

    The URL carries an opaque ImageServer asset id unrelated to the player id, so it
    can only be read off the page.
    """
    if not page:
        return None
    match = _PLAYER_IMAGE_RE.search(page)
    if not match:
        return None
    return html.unescape(match.group(1)).strip() or None


def parse_player_details(page):
    """Read every player-card field the scraper needs in a single pass."""
    birth_year, birth_month = parse_player_birth(page)
    return {
        "birth_year": birth_year,
        "birth_month": birth_month,
        "image_url": parse_player_image(page),
    }


def parse_team_fields(page):
    """Return [(field_id, field_name)] for a team's home grounds, outermost first.

    Read from the team page's own "רשימת מגרשים" block rather than from its fixture
    list, so away venues are never mistaken for the club's own ground.
    """
    if not page:
        return []
    block = _TEAM_FIELDS_BLOCK_RE.search(page)
    if not block:
        return []
    return [
        (field_id, html.unescape(name).strip())
        for field_id, name in _FIELD_LINK_RE.findall(block.group(1))
    ]


def parse_fields_directory(page):
    """Return one dict per ground in the association's grounds directory."""
    if not page:
        return []

    fields = []
    for chunk in _FIELD_ITEM_SPLIT_RE.split(page):
        head = _FIELD_ITEM_HEAD_RE.search(chunk)
        if not head:
            continue
        name_match = _FIELD_ITEM_NAME_RE.search(chunk)
        address_match = _FIELD_ITEM_ADDRESS_RE.search(chunk)
        raw_name = html.unescape(name_match.group(1)).strip() if name_match else ""
        fields.append(
            {
                "field_id": head.group(1),
                "region": head.group(2) or "",
                "field_name": _FIELD_CLOSED_RE.sub("", raw_name),
                "closed": bool(_FIELD_CLOSED_RE.match(raw_name)),
                "address": html.unescape(address_match.group(1)).strip()
                if address_match
                else "",
            }
        )
    return fields


def split_goals_by_competition(games):
    """Aggregate parsed game rows into per-competition-type goal counts."""
    totals = {LEAGUE: 0, CUP: 0, TOTO: 0}
    for game in games:
        totals[game["competition_type"]] += game["goals"]
    return totals
