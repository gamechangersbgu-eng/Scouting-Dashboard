"""Shared policy for ranking player-name evidence across IFA sources.

The Israeli Football Association masks some youth players' names as a run of
asterisks (``"*******"``) on at least some of the pages/endpoints this scraper
reads, even when other endpoints for the very same player publish a real name.
A masked or missing name must never be treated as better evidence than a real
one, so every place that picks *one* display name out of several observed rows
for the same player goes through this module instead of ad-hoc "first row" or
"non-empty" logic:

* ``ifa_scraper.run.aggregate_players`` (builds ``players_youth.csv``)
* ``dashboard.app_core.BaseScoutingData.player`` (merges
  ``player_season_stats.csv`` against ``player_history.csv`` at read time)
* the Postgres importer and ``PostgresScoutingData`` (same merge, sourced from
  the database instead of CSVs)

This is a merge *policy*, not a repair of missing upstream data: a masked name
is exactly what IFA published for that particular appearance, and the raw,
per-appearance record should keep it as scraped. What this module governs is
only which value wins when several appearances for the same player disagree.
"""

import re

_MASK_RE = re.compile(r"^\*+$")

KNOWN = "known"
MASKED = "masked"
MISSING = "missing"

_RANK = {MISSING: 0, MASKED: 1, KNOWN: 2}


def classify_name(value):
    """Return KNOWN, MASKED, or MISSING for a raw ``player_name`` value."""
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "nan":
        return MISSING
    if _MASK_RE.match(text):
        return MASKED
    return KNOWN


def name_rank(status):
    """Ordinal so KNOWN > MASKED > MISSING can be compared directly."""
    return _RANK[status]


def better_name(a, b):
    """Return whichever of two raw name values is the stronger evidence.

    On a tie in quality, ``a`` wins -- callers that care about recency should
    pass the value they want preferred on ties as ``a``.
    """
    a_text = "" if a is None else str(a).strip()
    b_text = "" if b is None else str(b).strip()
    if name_rank(classify_name(b_text)) > name_rank(classify_name(a_text)):
        return b_text
    return a_text


def best_name(candidates):
    """Pick the best ``(status, name)`` pair from an ordered iterable of raw values.

    Ties keep the *first* candidate of the winning quality, so passing
    candidates newest-first (as the rest of the pipeline already sorts them)
    gives stable, predictable results: the most recent known name wins over an
    older known name, but any known name beats a more recent masked one.
    """
    best_status, best_value = MISSING, ""
    for value in candidates:
        status = classify_name(value)
        if name_rank(status) > name_rank(best_status):
            best_status = status
            best_value = "" if value is None else str(value).strip()
    return best_status, best_value
