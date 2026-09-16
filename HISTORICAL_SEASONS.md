# Historical club seasons

The project separates **recent detailed statistics** from **full club history**.

- `python -m ifa_scraper.run` scrapes the three detailed-stat seasons: 2024/25,
  2025/26 and 2026/27.
- `python -m ifa_scraper.history` scrapes player-to-club appearances across the full
  configured youth/kids history window, currently 2010/11 through 2026/27.

The historical pass intentionally stores only identity/context fields (player, team,
season, age bracket and league). It does **not** pretend that old games/goals/minutes are
available when they were not collected. The dashboard joins the overlapping recent
history rows to `player_season_stats.csv`, so recent seasons retain their detailed stats
while older rows display `—` for unavailable statistics.

## Recommended workflow

```bash
# 1. Detailed current/recent statistics
python -m ifa_scraper.run

# 2. Full youth/kids club history
python -m ifa_scraper.history

# 3. Resolve home grounds for current and historical teams
python -m ifa_scraper.venues

# 4. Reload the dashboard
python -m dashboard.app
```

The history scraper discovers the actual youth/kids league index separately for each
season. This matters because old seasons contain divisions that are no longer present in
the site's current navigation.

Discovery is checkpointed in `data/league_index.csv`, and HTTP responses from the table
endpoints are cached under `data/cache/`, so subsequent runs do not repeat the expensive
league-id scan.

## Configured history seasons

| Season | ID |
|---|---:|
| 2026/27 | 28 |
| 2025/26 | 27 |
| 2024/25 | 26 |
| 2023/24 | 25 |
| 2022/23 | 24 |
| 2021/22 | 23 |
| 2020/21 | 22 |
| 2019/20 | 21 |
| 2018/19 | 20 |
| 2017/18 | 19 |
| 2016/17 | 18 |
| 2015/16 | 17 |
| 2014/15 | 16 |
| 2013/14 | 15 |
| 2012/13 | 14 |
| 2011/12 | 13 |
| 2010/11 | 12 |

The window begins at 2010/11 because the current player dataset includes a small number
of players born in 2005; this reaches approximately age five for that oldest cohort.

You can restrict the history run when debugging, for example:

```bash
python -m ifa_scraper.history --seasons 28 27 26 25 24
```
