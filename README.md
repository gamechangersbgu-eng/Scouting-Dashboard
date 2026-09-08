# IFA Youth Player Scraper and Scouting Dashboard

Scrapes player statistics for every team in the Israeli Football Association's youth
leagues (נוער, נערים א, נערים ב, נערים ג) from [football.org.il](https://www.football.org.il/),
produces one CSV row per unique player ID, and serves a local scouting dashboard over
the result.

## Quick start

```bash
pip install -r requirements.txt
python -m ifa_scraper.run       # scrape players (slow, resumable)
python -m ifa_scraper.venues    # locate every team on the map
python -m dashboard.app         # open the dashboard
```

Outputs land in `data/`:

| File | Contents |
| --- | --- |
| `players_youth.csv` | The deliverable: one row per unique `player_id` |
| `player_season_stats.csv` | Per player-team-season detail, so any aggregate can be traced back |
| `player_details.csv` | Birth year, birth month and photo URL per player; also the phase 4 resume checkpoint |
| `team_locations.csv` | One row per team with its home ground, address and coordinates |
| `team_fields.csv` | Team-to-ground mapping; the resume checkpoint for `venues` |
| `geonames_IL.txt`, `geocode_cache.json` | Cached gazetteer and geocoding results |

Useful flags:

```bash
python -m ifa_scraper.run --seasons 27 26         # limit to specific season ids
python -m ifa_scraper.run --skip-goal-split       # skip phase 3 (leaves goals_league/goals_cup at 0)
python -m ifa_scraper.run --skip-player-details   # skip phase 4 (leaves birth_year/image_url empty)
python -m ifa_scraper.run --no-cache              # bypass the on-disk response cache
python -m ifa_scraper.venues --refresh-grounds    # re-fetch team-to-ground instead of resuming
python -m dashboard.app --port 8000 --no-browser  # serve on another port, don't open a browser
```

Responses are cached under `data/cache/`, so an interrupted run resumes almost instantly.
Delete that directory to force a refresh. Player pages are the exception: they are ~150KB
each and only three fields are read from them, so rather than caching the HTML the
extracted values are appended to `data/player_details.csv` and reused on later runs.

Only one run may be active at a time. Runs share their checkpoint files, so concurrent
runs would interleave rows and corrupt them; a lock at `data/.scrape.lock` prevents this
and is cleared automatically on exit (including after a crash, via a stale-PID check).

## Coverage

42 youth leagues (11 נוער, 9 נערים א, 11 נערים ב, 11 נערים ג) across three seasons:
2024/25, 2025/26 and 2026/27. Note that 2026/27 had only just started at the time of
scraping, so it contributes current team affiliations more than accumulated statistics.

## Data source

The site is ASP.NET WebForms and loads its tables over AJAX, so rather than scraping
rendered HTML the scraper calls the same internal web services the site's own JavaScript
uses:

| Endpoint | Purpose |
| --- | --- |
| `Components.asmx/League_AllTables` | League table for a league-season, yielding team IDs and names |
| `Components.asmx/GetTeamPlayersStatisticsList` | A full squad's season statistics in a single request |
| `Components.asmx/GetPlayerGames` | A player's individual games, used to split goals by competition |
| `/players/player/?player_id=` | The player card, read for date of birth and photo URL |
| `/team-details/?team_id=` | The team page, read for the club's home ground |
| `/association/fields/` | The grounds directory, read for each ground's street address |

Minutes played are published by the association directly, so `minutes_total` and
`avg_minutes_per_game` are official figures rather than estimates derived from
substitution times.

## Output schema (`players_youth.csv`)

| Column | Meaning |
| --- | --- |
| `player_id` | The association's unique player ID |
| `player_name` | Name as published (surname first), taken from the most recent season |
| `birth_year` | Year of birth. Empty when the association publishes no date of birth |
| `current_team` | Most recent team |
| `teams_history` | Every team spell, ordered current to first, as `Team (season)` separated by ` \| ` |
| `num_teams` | Number of distinct teams |
| `age_groups` | Age brackets the player appeared in (נוער / נערים א / ב / ג) |
| `plays_above_age` | `Yes` if, in their most recent season, they played in a bracket above their age |
| `age_groups_above` | How many brackets above their age, in the most recent season (0 if none) |
| `above_age_history` | Every above-age spell, e.g. `נערים א 2025/26 (+1)`. Empty if never |
| `leagues` | Leagues the player appeared in |
| `seasons` | Seasons covered for this player |
| `goals_total` | Total goals across all youth teams and competitions |
| `goals_league` | Goals in league competition |
| `goals_cup` | Goals in cup competition (includes Toto/League Cup) |
| `games_total` | Appearances |
| `minutes_total` | Total minutes played |
| `avg_minutes_per_game` | `minutes_total / games_total`, rounded to one decimal |
| `starts` | Games started (הרכב פותח) |
| `sub_on` | Games entered as a substitute (נכנס כמחליף) |
| `sub_off` | Games substituted off (הוחלף) |
| `yellow_cards_league_cup` | Yellow cards in league and national cup |
| `yellow_cards_toto` | Yellow cards in the Toto/League Cup |
| `yellow_cards_total` | Sum of the two yellow card columns |
| `red_cards` | Red cards |
| `image_url` | Direct URL to the player's photo. Empty if the player card has no image |

CSVs are written as `utf-8-sig` so the Hebrew columns open correctly in Excel.

## The scouting dashboard

`python -m dashboard.app` serves a single page on `http://127.0.0.1:5000/`. Pick a player
and it shows their career totals, whether they are playing above their age group, a
season-by-season table, and a map of every place they have played.

On the map, **the earlier the club, the bigger the marker**, and a dashed line joins the
places in career order. Markers are numbered from earliest to most recent; amber marks a
spell above age and green the current club.

Markers are grouped by place rather than by team. A club enters a separate squad per age
bracket, each with its own team id, so a player who moves up a bracket at the same club
would otherwise stack several identical markers on one point. The popup lists every squad
at that place, and the season table keeps the per-squad detail.

The CSVs are read into memory once at startup, so the dashboard needs no database and no
network beyond the map tiles. Leaflet is vendored under `dashboard/static/vendor/`.

## How team locations are derived

The association publishes no club location and no coordinates: its own grounds map
geocodes in the browser and ships `-1` placeholders. Locations are therefore assembled in
three steps: the team page gives the club's home ground, the grounds directory gives that
ground's street address, and the address is resolved to coordinates.

That last step is the one that can go quietly wrong, because a free-text geocoder will
answer *something* for any input. Addresses are published as `<street> <number> <locality>`
with no delimiter, so the locality has to be recovered from the end of the string, and
handing partial addresses to a geocoder invites confident nonsense: `נתיב דבורה ערד`
resolves to the village of Daburiyya, 165km from Arad.

So the locality is matched against a closed list of real localities instead, taken from
the [GeoNames](https://www.geonames.org/) Israel extract and indexed by Hebrew name. The
longest matching suffix of the address wins, which picks רמת גן over a place called גן,
and cannot invent Daburiyya because `דבורה ערד` is not a locality. Spelling differences
between the two sources are folded away (`סח'נין` / `סח’נין` / `סכנין`,
`תל אביב יפו` / `תל אביב-יפו`, and `ע'` for the Arabic غ where GeoNames writes `ג`).

Nominatim is then used only to sharpen a known locality to a street, and its answer is
discarded unless it lands within 12km of the locality. That guard rejected 43 street
matches, among them `אחד העם 8 רמת גן` pointing 141km into the Negev. Where it is
rejected, or where there is no street match, the locality centre is used.

The result for the current data is 782 of 782 teams located: 651 at street level, 91 at
locality level, and 40 through a fallback lookup for localities GeoNames does not list
(mostly Area C, which it files outside Israel). Every located team is within 12km of its
own published locality.

| `team_locations.csv` column | Meaning |
| --- | --- |
| `field_id`, `field_name` | The club's home ground, from the team page |
| `address` | The ground's street address, from the grounds directory |
| `city` | Resolved locality name |
| `lat`, `lon` | Coordinates |
| `geocode_query` | The string that actually produced the match, for auditing |
| `precision` | `street`, `locality` or `fallback` |

## Known limitations

**Assists are not included, because the association does not publish them.** This was
verified across the player page, the squad statistics table, the match lineup page and the
goal-by-goal match timeline: goal events record the scorer only, never an assister. There
is no assist data on the site to scrape.

Two further notes on how the numbers are scoped:

- Squad statistics are per team, so a player who moved clubs mid-season has his totals
  summed across every youth team he appeared for.
- The per-game feed used for the league/cup split covers a player's entire season,
  including senior and children's football (some players also appear in a senior league
  such as ליגה ג' or a ליגת ילדים side). It is filtered to youth competitions so the split
  stays consistent with the youth-scoped totals.

`goals_total` comes from the association's own per-team season aggregates, while
`goals_league` and `goals_cup` are derived from its game-by-game feed. Those two upstream
sources do not always agree: `goals_league + goals_cup` equals `goals_total` for 97.7% of
scorers, and is within one goal for 99.2%. The residual is an inconsistency in the source
data rather than in parsing, so it is left visible instead of being reconciled away.

## Layout

```
ifa_scraper/
  config.py     league IDs by age bracket, season map, tuning knobs
  client.py     HTTP session with DNS override, retry/backoff and disk cache
  parse.py      parsers for the site's endpoints and pages
  run.py        four-phase player scrape and CSV output
  venues.py     team -> home ground -> address -> coordinates
  gazetteer.py  Hebrew locality index built from the GeoNames Israel extract
  geocode.py    distance-validated Nominatim lookups, cached on disk
dashboard/
  app.py        Flask app: static page plus a small read-only JSON API
  static/       the page, its stylesheet, its script and vendored Leaflet
```

`client.py` pins the hostname to a single resolved IP for the whole run, because some
resolvers return SERVFAIL for this domain and even a resolver that works at startup can
fail mid-run. It resolves once (system resolver, then a public DNS server, then known
Cloudflare addresses) and rotates to the next candidate if a connection fails.
Hostname-based TLS verification is preserved throughout.

The photo URL points at the association's `ImageServer` and carries an opaque asset id
that is unrelated to `player_id`, so it can only be read from the player page rather than
constructed.

## How "playing above age" is decided

Youth brackets overlap rather than mapping one-to-one onto birth years: each bracket is
roughly 80% one birth year plus a ~20% slice of the year below. There is therefore no
published cutoff to compare against, so the reference is taken from the data itself.

For every birth year in every season, the natural bracket is the one holding the majority
of that cohort. A player is flagged when they appear in a bracket further up the ladder
(`נערים ג` to `נערים ב` to `נערים א` to `נוער`) than their own cohort's natural bracket, and
`age_groups_above` records how many steps up.

Worked example: in 2024/25, players born 2010 are 81% in `נערים ג` and 19% in `נערים ב`, so
`נערים ג` is natural for that cohort and the 19% in `נערים ב` are flagged `+1`.

Because `נוער` is the oldest bracket, players older than its main cohort are never flagged;
they are at or above age rather than playing up. Where a player turns out in two brackets
in the same season, the flag reflects the highest one reached.
