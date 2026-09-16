# Historical seasons

The scraper now runs with the **current season only** by default (`2026/27`).

To scrape earlier seasons, pass their season IDs explicitly:

```bash
# 2025/26
python -m ifa_scraper.run --seasons 27

# 2024/25
python -m ifa_scraper.run --seasons 26

# Both earlier seasons
python -m ifa_scraper.run --seasons 27 26
```

Season IDs:

| Season | ID |
|---|---:|
| 2026/27 | 28 |
| 2025/26 | 27 |
| 2024/25 | 26 |

After scraping, restart the dashboard so it reloads the CSV files:

```bash
python -m dashboard.app
```

The default configuration is in `ifa_scraper/config.py` under `SEASONS`.
