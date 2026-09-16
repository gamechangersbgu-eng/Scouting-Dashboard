# Historical seasons

The scraper runs with the **current season only** by default (`2026/27`).

To scrape earlier seasons, pass their season IDs explicitly. The historical range currently supported by the configuration goes back to **2022/23**.

```bash
# 2025/26
python -m ifa_scraper.run --seasons 27

# 2024/25
python -m ifa_scraper.run --seasons 26

# 2023/24
python -m ifa_scraper.run --seasons 25

# 2022/23
python -m ifa_scraper.run --seasons 24

# All seasons from 2022/23 through the current season
python -m ifa_scraper.run --seasons 28 27 26 25 24
```

Season IDs:

| Season | ID |
|---|---:|
| 2026/27 | 28 |
| 2025/26 | 27 |
| 2024/25 | 26 |
| 2023/24 | 25 |
| 2022/23 | 24 |

After scraping, restart the dashboard so it reloads the CSV files:

```bash
python -m dashboard.app
```

The default configuration is in `ifa_scraper/config.py` under `SEASONS`.
