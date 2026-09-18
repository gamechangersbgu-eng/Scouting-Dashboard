"""Import the CSV canonical files into a new, not-yet-published Postgres dataset.

Creates one new ``dataset_versions`` row and imports under its ``dataset_id``.
Never marks the dataset live -- that is ``scripts/publish_dataset.py``'s job,
run only after this script's own validation has passed. Safe to re-run: every
run creates a fresh dataset_id, so a failed or abandoned import is simply
left behind (status ``failed`` or ``building``) rather than corrupting
anything live.

Reads, from ``--data-dir`` (default ``ifa_scraper.config.DATA_DIR``):

* ``player_season_stats.csv`` / ``player_history.csv`` -- fact rows.
  ``source_file`` is provenance only, never football identity (see the
  schema migration's "Why this grain, and not source_file or league_id"):
  the two sources are merged into exactly one row per (player_id, team_id,
  season_id) here, via ``dashboard.app_core.merge_canonical_rows`` -- the
  same stats-quality-ranking, masked-name-safe algorithm
  ``BaseScoutingData.player()`` already uses to merge them at read time for
  the CSV path, so the two call sites cannot silently drift apart.
* ``player_details.csv`` -- birth_year / image_url per player.
* ``team_fields.csv`` -- the authoritative team_id -> field_id mapping.
* ``team_locations.csv`` -- geocoded enrichment of the same field_ids
  (fewer rows than team_fields.csv: only the teams a geocoding run reached).

Deliberately NOT read: ``players_youth.csv``. It is a derived, pre-aggregated
view built by the scraper for the CSV-only dashboard, not a canonical fact
source -- see the schema migration's docstring for the full reasoning. The
Postgres ``players`` dimension table is built here directly from the season
and history rows instead, the same way ``ifa_scraper.run.aggregate_players``
already does for ``players_youth.csv`` itself.

league_id handling: a (team_id, season_id) can be discovered under more than
one league listing (see the schema migration), and the CSV's own
``league_id``/``league_name`` columns are comma-joined text -- for a small
minority of rows (153 of 194,518 in this repository's own
player_history.csv), older scraper runs may have paired that id list with
that name list in the wrong order (fixed going forward in
``ifa_scraper.run.phase1_collect_teams``, but not retroactively fixable from
already-comma-joined text). So this importer only trusts an id->name pairing
when a row's league_id is single-valued (unambiguous); a league_id seen only
ever inside a multi-valued row gets a placeholder name here rather than a
guessed one. Membership itself (which league_ids a team-season belongs to)
is always safe to record either way, since it is just a set of ids, not a
positional pairing -- see ``_upsert_leagues_and_memberships``.

Usage:
    python -m scripts.import_canonical_dataset [--data-dir PATH] [--scraper-git-sha SHA]
"""

import argparse
import csv
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from dashboard.app_core import merge_canonical_rows
from ifa_scraper import config, db, name_quality, validation

log = logging.getLogger("scripts.import_canonical_dataset")

FACT_COLUMNS = [
    "games", "goals", "minutes", "starts", "sub_on", "sub_off",
    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
]


def _int_or_none(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _text_or_none(value):
    text = (value or "").strip()
    return text or None


def _bool_from_csv(value, default=False):
    text = (value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes"}


def _optional_bool(value):
    """Like ``_bool_from_csv`` but with no default: absent stays unknown (None).

    Used only for observation rows, which must stay lossless -- a source that
    never published a ``stats_available`` column (``player_season_stats.csv``,
    confirmed against this repository's own copy) has to record that absence
    as SQL NULL, not a coerced ``False``, so that
    ``BaseScoutingData._normalise_stat_rows`` can still infer it the same way
    at read time as it does for the CSV itself. See the 0002 migration's
    docstring.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    return text in {"1", "true", "yes"}


def _read_csv(path):
    if not path.exists():
        log.warning("%s missing; treating as zero rows", path.name)
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_season_source(path, source_file):
    """Read one SEASON_COLUMNS-shaped CSV into fact rows, de-duplicating by natural key.

    A true duplicate natural key within one source file would indicate a
    scraper bug (the IFA endpoint is already aggregated per player/team/season
    -- see dashboard.app_core._canonical_key), so it is logged rather than
    silently dropped; the later of the two rows wins, matching the scraper's
    own "newest observation wins" convention elsewhere.

    Each surviving row also gets ``_source_row_number``: its 0-based position
    in this exact CSV file, i.e. pandas.read_csv's row order for the same
    file. This is the only lossless way to reproduce CsvScoutingData's
    tie-breaking behavior on the Postgres side (see the 0003 migration's
    docstring) -- BaseScoutingData.player() and ifa_scraper.run.aggregate_players()
    both do a *stable* sort by (season_id, team_name), and when two rows
    share both (which happens for real -- two team_ids with the byte-for-byte
    identical team_name), the stable sort falls through to whatever order the
    rows arrived in. For CsvScoutingData that is pandas.read_csv's row order;
    PostgresScoutingData can only reproduce that if the importer records it
    explicitly, since plain SQL makes no row-order promise at all.

    Critically, this must be the row's *own* file position, not its
    surviving key's first-seen position: ``seen[key] = row`` on a duplicate
    natural key overwrites the value but -- since dict key order is
    insertion order, not last-write order -- leaves the key sitting wherever
    it first appeared. Enumerating ``seen.values()`` after the loop would
    therefore hand every de-duplicated row the position of its *first*,
    discarded occurrence, not the later, actually-kept one. Storing
    ``(raw_index, row)`` per key instead means a duplicate overwrites both
    together, so the row that wins the dedup also keeps its own, correct
    file position.
    """
    seen = {}
    duplicates = 0
    for raw_index, row in enumerate(_read_csv(path)):
        key = (row.get("player_id"), row.get("team_id"), _int_or_none(row.get("season_id")))
        if key in seen:
            duplicates += 1
        seen[key] = (raw_index, row)
    if duplicates:
        log.warning("%s: %s duplicate natural-key rows collapsed (kept the later row)", path.name, duplicates)
    rows = []
    for raw_index, row in seen.values():
        row["_source_file"] = source_file
        row["_source_row_number"] = raw_index
        rows.append(row)
    return rows


def _set_local_statement_timeout(cursor):
    # Scope this to the importer transaction only; it is reset automatically at
    # COMMIT/ROLLBACK and does not alter the database-wide statement_timeout.
    cursor.execute("SET LOCAL statement_timeout = '30min'")


def _bulk_insert_many(cursor, sql, rows):
    if not rows:
        return 0
    if hasattr(cursor, "executemany"):
        cursor.executemany(sql, rows)
        return len(rows)
    for row in rows:
        cursor.execute(sql, row)
    return len(rows)


def _bulk_copy_rows(cursor, table_name, columns, rows):
    if not rows:
        return 0
    if hasattr(cursor, "copy"):
        with cursor.copy(f"COPY {table_name} ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        return len(rows)
    placeholders = ", ".join(["%s"] * len(columns.split(", ")))
    cursor.executemany(
        f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def _upsert_seasons(cursor, all_rows):
    start = time.perf_counter()
    seasons = {}
    for row in all_rows:
        season_id = _int_or_none(row.get("season_id"))
        label = _text_or_none(row.get("season"))
        if season_id is not None and label:
            seasons[season_id] = label
    season_rows = [(season_id, label) for season_id, label in seasons.items()]
    _bulk_insert_many(
        cursor,
        """
        INSERT INTO seasons (season_id, label) VALUES (%s, %s)
        ON CONFLICT (season_id) DO UPDATE SET label = EXCLUDED.label
        """,
        season_rows,
    )
    log.info("phase seasons: %.3fs (%s rows)", time.perf_counter() - start, len(season_rows))


def _load_venue_sources(data_dir):
    """Return (team_id -> field_id/field_name, field_id -> geocoded venue)."""
    team_fields = {}
    for row in _read_csv(data_dir / "team_fields.csv"):
        field_id = _text_or_none(row.get("field_id"))
        team_id = _text_or_none(row.get("team_id"))
        if team_id and field_id:
            team_fields[team_id] = {
                "field_id": field_id,
                "field_name": _text_or_none(row.get("field_name")),
            }

    venues_by_field = {}
    for row in _read_csv(data_dir / "team_locations.csv"):
        field_id = _text_or_none(row.get("field_id"))
        if not field_id or field_id in venues_by_field:
            continue
        venues_by_field[field_id] = {
            "field_name": _text_or_none(row.get("field_name")),
            "city": _text_or_none(row.get("city")),
            "address": _text_or_none(row.get("address")),
            "lat": float(row["lat"]) if _text_or_none(row.get("lat")) else None,
            "lon": float(row["lon"]) if _text_or_none(row.get("lon")) else None,
            "precision": _text_or_none(row.get("precision")),
        }
    return team_fields, venues_by_field


def _upsert_venues_and_teams(cursor, all_rows, data_dir):
    start = time.perf_counter()
    team_fields, venues_by_field = _load_venue_sources(data_dir)

    distinct_field_ids = {mapping["field_id"] for mapping in team_fields.values()}
    venue_rows = []
    for field_id in distinct_field_ids:
        geocoded = venues_by_field.get(field_id, {})
        field_name = geocoded.get("field_name") or next(
            (m["field_name"] for m in team_fields.values() if m["field_id"] == field_id), None
        )
        venue_rows.append(
            (
                field_id,
                field_name,
                geocoded.get("city"),
                geocoded.get("address"),
                geocoded.get("lat"),
                geocoded.get("lon"),
                geocoded.get("precision"),
            )
        )
    _bulk_insert_many(
        cursor,
        """
        INSERT INTO venues (field_id, field_name, city, address, lat, lon, precision)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (field_id) DO UPDATE SET
            field_name = EXCLUDED.field_name,
            city = EXCLUDED.city,
            address = EXCLUDED.address,
            lat = EXCLUDED.lat,
            lon = EXCLUDED.lon,
            precision = EXCLUDED.precision
        """,
        venue_rows,
    )
    log.info("phase venues: %.3fs (%s rows)", time.perf_counter() - start, len(venue_rows))

    team_start = time.perf_counter()
    # Newest season first per team_id, so the most recent team_name wins --
    # the same convention ifa_scraper.run.aggregate_players uses for a
    # player's current_team.
    rows_by_team = defaultdict(list)
    for row in all_rows:
        team_id = _text_or_none(row.get("team_id"))
        if team_id:
            rows_by_team[team_id].append(row)

    team_rows = []
    for team_id, rows in rows_by_team.items():
        rows.sort(key=lambda r: -(_int_or_none(r.get("season_id")) or 0))
        latest_name = next((_text_or_none(r.get("team_name")) for r in rows if _text_or_none(r.get("team_name"))), team_id)
        field_id = (team_fields.get(team_id) or {}).get("field_id")
        team_rows.append((team_id, latest_name, field_id))
    _bulk_insert_many(
        cursor,
        """
        INSERT INTO teams (team_id, latest_name, current_field_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (team_id) DO UPDATE SET
            latest_name = EXCLUDED.latest_name,
            current_field_id = COALESCE(EXCLUDED.current_field_id, teams.current_field_id)
        """,
        team_rows,
    )
    log.info("phase teams: %.3fs (%s rows)", time.perf_counter() - team_start, len(team_rows))


def _upsert_dataset_team_locations(cursor, dataset_id, data_dir):
    """Snapshot team_locations.csv verbatim, one row per team_id, for this dataset.

    Deliberately NOT sourced from ``_load_venue_sources()``/``venues_by_field``
    above: that dict is keyed by field_id and only keeps the first
    team_locations.csv row seen for a shared field_id, which silently
    discards a real, distinct city for every other team_id at that field
    (confirmed against this repository's own data -- field_id 48 alone is
    shared by 11 team_ids, most geocoded to "מג'ד אל כרום" but team_id 6480
    to a different, more specific "מגדל"; see the 0003 migration's
    docstring). ``CsvScoutingData._load_locations()`` never goes through that
    field_id dedup either -- it reads team_locations.csv straight, keyed by
    team_id -- so this is the only way ``PostgresScoutingData`` can reproduce
    the same per-team city ``_infer_likely_origin()`` sees. team_id is
    confirmed unique in this repository's own team_locations.csv (2,774 rows,
    2,774 distinct team_ids), so no de-duplication policy is needed here.
    """
    start = time.perf_counter()
    location_rows = []
    for row in _read_csv(data_dir / "team_locations.csv"):
        team_id = _text_or_none(row.get("team_id"))
        if not team_id:
            continue
        lat_text = _text_or_none(row.get("lat"))
        lon_text = _text_or_none(row.get("lon"))
        location_rows.append(
            (
                dataset_id,
                team_id,
                _text_or_none(row.get("field_id")),
                _text_or_none(row.get("field_name")),
                _text_or_none(row.get("city")),
                _text_or_none(row.get("address")),
                float(lat_text) if lat_text else None,
                float(lon_text) if lon_text else None,
                _text_or_none(row.get("precision")),
            )
        )
    _bulk_copy_rows(
        cursor,
        "dataset_team_locations",
        "dataset_id, team_id, field_id, field_name, city, address, lat, lon, precision",
        location_rows,
    )
    log.info(
        "phase dataset_team_locations: %.3fs (%s rows)",
        time.perf_counter() - start, len(location_rows),
    )
    return len(location_rows)


def _split_league_ids(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _upsert_leagues_and_memberships(cursor, dataset_id, all_rows):
    """Populate ``leagues`` and the normalized ``team_season_leagues`` junction.

    Reads every row's raw (possibly comma-joined) league_id/league_name text,
    which is never written anywhere in the schema itself -- see the module
    docstring for why only single-valued rows are trusted for id->name
    pairing, while membership (the junction rows) is always safe to record.
    """
    start = time.perf_counter()
    single_valued_names = {}
    memberships = defaultdict(set)  # (team_id, season_id) -> {league_id, ...}

    for row in all_rows:
        team_id = _text_or_none(row.get("team_id"))
        season_id = _int_or_none(row.get("season_id"))
        if not team_id or season_id is None:
            continue
        league_ids = _split_league_ids(row.get("league_id"))
        if not league_ids:
            continue
        memberships[(team_id, season_id)].update(league_ids)
        if len(league_ids) == 1:
            name = _text_or_none(row.get("league_name"))
            if name:
                single_valued_names[league_ids[0]] = name

    all_league_ids = {lid for ids in memberships.values() for lid in ids}
    league_rows = [(int(league_id), single_valued_names.get(league_id) or f"League {league_id}") for league_id in all_league_ids]
    _bulk_insert_many(
        cursor,
        """
        INSERT INTO leagues (league_id, latest_name) VALUES (%s, %s)
        ON CONFLICT (league_id) DO UPDATE SET latest_name = EXCLUDED.latest_name
        """,
        league_rows,
    )
    log.info(
        "phase leagues: %.3fs (%s rows, %s resolved from an unambiguous single-league row)",
        time.perf_counter() - start,
        len(league_rows),
        len(single_valued_names),
    )

    membership_rows = []
    for (team_id, season_id), league_ids in memberships.items():
        for league_id in league_ids:
            membership_rows.append((dataset_id, team_id, season_id, int(league_id)))
    _bulk_insert_many(
        cursor,
        """
        INSERT INTO team_season_leagues (dataset_id, team_id, season_id, league_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (dataset_id, team_id, season_id, league_id) DO NOTHING
        """,
        membership_rows,
    )
    log.info("phase team_season_leagues: %.3fs (%s rows)", time.perf_counter() - start, len(membership_rows))


def _upsert_players(cursor, all_rows, data_dir):
    start = time.perf_counter()
    names_by_player = defaultdict(list)
    for row in all_rows:
        player_id = _text_or_none(row.get("player_id"))
        if player_id:
            names_by_player[player_id].append(row.get("player_name"))

    details = {}
    for row in _read_csv(data_dir / "player_details.csv"):
        player_id = _text_or_none(row.get("player_id"))
        if player_id:
            details[player_id] = {
                "birth_year": _int_or_none(row.get("birth_year")),
                "image_url": _text_or_none(row.get("image_url")),
            }

    # players.name_status must never regress from a rank an earlier import
    # already established (known must never be displaced by a later masked
    # observation) -- see ifa_scraper.validation._check_no_masked_over_known_regression,
    # which guards this same invariant from the other direction.
    cursor.execute("SELECT player_id, name_status FROM players")
    existing_status = {player_id: status for player_id, status in cursor.fetchall()}

    update_rows = []
    insert_rows = []
    for player_id, names in names_by_player.items():
        status, name = name_quality.best_name(names)
        previous_status = existing_status.get(player_id)
        detail = details.get(player_id) or {}
        payload = {
            "player_id": player_id,
            "name": name or player_id,
            "status": status,
            "birth_year": detail.get("birth_year"),
            "image_url": detail.get("image_url"),
        }
        if previous_status and name_quality.name_rank(previous_status) > name_quality.name_rank(status):
            # Keep the existing (better) name/status; only birth_year/image_url refresh.
            update_rows.append((payload["birth_year"], payload["image_url"], payload["player_id"]))
        else:
            insert_rows.append((payload["player_id"], payload["name"], payload["status"], payload["birth_year"], payload["image_url"]))

    if update_rows:
        _bulk_insert_many(
            cursor,
            """
            UPDATE players SET
                birth_year = COALESCE(%s, birth_year),
                image_url = COALESCE(%s, image_url)
            WHERE player_id = %s
            """,
            update_rows,
        )
    if insert_rows:
        _bulk_insert_many(
            cursor,
            """
            INSERT INTO players (player_id, latest_name, name_status, birth_year, image_url)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (player_id) DO UPDATE SET
                latest_name = EXCLUDED.latest_name,
                name_status = EXCLUDED.name_status,
                birth_year = COALESCE(EXCLUDED.birth_year, players.birth_year),
                image_url = COALESCE(EXCLUDED.image_url, players.image_url)
            """,
            insert_rows,
        )
    log.info("phase players: %.3fs (%s rows)", time.perf_counter() - start, len(names_by_player))


def _insert_facts(cursor, dataset_id, merged_rows):
    """Insert the already-merged (one row per grain) canonical fact rows.

    ``merged_rows`` must already be de-duplicated to at most one row per
    (player_id, team_id, season_id) -- see ``merge_canonical_rows`` in
    ``import_dataset`` below. ``source_file`` here records which source's
    stats won that merge; it is provenance, not part of the table's
    UNIQUE constraint (see the schema migration).
    """
    start = time.perf_counter()
    payload = []
    for row in merged_rows:
        payload.append(
            (
                dataset_id,
                row.get("player_id"),
                row.get("team_id"),
                _int_or_none(row.get("season_id")),
                row["_source_file"],
                _text_or_none(row.get("player_name")),
                name_quality.classify_name(row.get("player_name")),
                _text_or_none(row.get("team_name")),
                _text_or_none(row.get("age_group")),
                _text_or_none(row.get("league_name")),
                _int_or_none(row.get("games")),
                _int_or_none(row.get("goals")),
                _int_or_none(row.get("minutes")),
                _int_or_none(row.get("starts")),
                _int_or_none(row.get("sub_on")),
                _int_or_none(row.get("sub_off")),
                _int_or_none(row.get("yellow_cards_league_cup")),
                _int_or_none(row.get("yellow_cards_toto")),
                _int_or_none(row.get("red_cards")),
                _bool_from_csv(row.get("stats_available")),
                _text_or_none(row.get("stats_source")),
                _text_or_none(row.get("stats_completeness")) or "unavailable",
            )
        )

    copy_start = time.perf_counter()
    row_count = _bulk_copy_rows(
        cursor,
        "player_team_seasons",
        "dataset_id, player_id, team_id, season_id, source_file, player_name, name_status, team_name, age_group, league_name, games, goals, minutes, starts, sub_on, sub_off, yellow_cards_league_cup, yellow_cards_toto, red_cards, stats_available, stats_source, stats_completeness",
        payload,
    )
    log.info("phase player_team_seasons COPY: %.3fs (%s rows)", time.perf_counter() - copy_start, row_count)
    return row_count


def _insert_observations(cursor, dataset_id, all_rows):
    """Insert every raw, unmerged source row into the lossless observation layer.

    Unlike ``_insert_facts`` (which takes ``merged_rows``, at most one per
    grain), this takes ``all_rows`` -- ``season_rows + history_rows``,
    entirely unmerged -- and writes one row per (player_id, team_id,
    season_id, source_file). This must run against the same rows the merge
    itself consumes, and it must run regardless of what the merge decides,
    so that both sources stay independently queryable even where their
    canonical keys collide (49,379 of 49,593 recent rows, in this
    repository's own data). See the 0002 migration's docstring.

    ``stats_available``/``stats_source``/``stats_completeness`` are written
    with ``_optional_bool``/``_text_or_none`` (no coerced defaults) rather
    than ``_bool_from_csv``/``"unavailable"`` fallback used for the merged
    canonical table -- a source that never published those columns
    (``player_season_stats.csv``) must record that as NULL here, not a
    fabricated value, so ``_normalise_stat_rows`` can still infer it
    correctly at read time exactly as it does for the CSV path.
    """
    start = time.perf_counter()
    payload = []
    for row in all_rows:
        payload.append(
            (
                dataset_id,
                row.get("player_id"),
                row.get("team_id"),
                _int_or_none(row.get("season_id")),
                row["_source_file"],
                _text_or_none(row.get("player_name")),
                _text_or_none(row.get("team_name")),
                _text_or_none(row.get("age_group")),
                _text_or_none(row.get("league_name")),
                _int_or_none(row.get("games")),
                _int_or_none(row.get("goals")),
                _int_or_none(row.get("minutes")),
                _int_or_none(row.get("starts")),
                _int_or_none(row.get("sub_on")),
                _int_or_none(row.get("sub_off")),
                _int_or_none(row.get("yellow_cards_league_cup")),
                _int_or_none(row.get("yellow_cards_toto")),
                _int_or_none(row.get("red_cards")),
                _optional_bool(row.get("stats_available")),
                _text_or_none(row.get("stats_source")),
                _text_or_none(row.get("stats_completeness")),
                row.get("_source_row_number"),
            )
        )

    copy_start = time.perf_counter()
    row_count = _bulk_copy_rows(
        cursor,
        "player_team_season_observations",
        "dataset_id, player_id, team_id, season_id, source_file, player_name, team_name, age_group, league_name, games, goals, minutes, starts, sub_on, sub_off, yellow_cards_league_cup, yellow_cards_toto, red_cards, stats_available, stats_source, stats_completeness, source_row_number",
        payload,
    )
    log.info(
        "phase player_team_season_observations COPY: %.3fs (%s rows)",
        time.perf_counter() - copy_start, row_count,
    )
    log.info("phase player_team_season_observations total: %.3fs", time.perf_counter() - start)
    return row_count


def import_dataset(database_url=None, data_dir=None, scraper_git_sha=None):
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR

    season_rows = _load_season_source(data_dir / "player_season_stats.csv", "player_season_stats")
    history_rows = _load_season_source(data_dir / "player_history.csv", "player_history")
    all_rows = season_rows + history_rows
    if not season_rows:
        raise RuntimeError(f"{data_dir / 'player_season_stats.csv'} produced no rows; refusing to import")

    # One row per (player_id, team_id, season_id): source_file is provenance,
    # never identity, so the two sources are merged here -- before insert --
    # with the exact algorithm BaseScoutingData.player() uses at read time for
    # the CSV path, rather than smuggling source_file into a uniqueness key.
    # History first, season/recent second: merge_canonical_rows() keeps the
    # later row on a stats-quality tie, matching player()'s own convention.
    merged_rows = list(merge_canonical_rows(history_rows + season_rows).values())
    log.info(
        "merged %s raw rows (%s season + %s history) into %s canonical rows",
        len(all_rows), len(season_rows), len(history_rows), len(merged_rows),
    )

    with db.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO dataset_versions (status, scraper_git_sha) VALUES ('building', %s) RETURNING dataset_id",
                (scraper_git_sha,),
            )
            (dataset_id,) = cursor.fetchone()
            log.info("building dataset_id=%s", dataset_id)

            _set_local_statement_timeout(cursor)
            _upsert_seasons(cursor, all_rows)
            _upsert_venues_and_teams(cursor, all_rows, data_dir)
            _upsert_dataset_team_locations(cursor, dataset_id, data_dir)
            _upsert_players(cursor, all_rows, data_dir)
            _upsert_leagues_and_memberships(cursor, dataset_id, all_rows)
            # Observations first: they must record every raw row regardless of
            # what the merge below decides, so a bug in the merge can never
            # take the lossless layer down with it.
            observation_count = _insert_observations(cursor, dataset_id, all_rows)
            log.info("inserted %s player_team_season_observations rows", observation_count)
            fact_count = _insert_facts(cursor, dataset_id, merged_rows)
            log.info("inserted %s player_team_seasons rows", fact_count)

            cursor.execute(
                "UPDATE dataset_versions SET row_counts = %s WHERE dataset_id = %s",
                (
                    json.dumps(
                        {
                            "player_team_seasons_rows": fact_count,
                            "player_team_season_observations_rows": observation_count,
                            "raw_season_rows": len(season_rows),
                            "raw_history_rows": len(history_rows),
                            "rows_merged_away": len(all_rows) - fact_count,
                        }
                    ),
                    dataset_id,
                ),
            )
        connection.commit()

    # Validate in its own connection/transaction, after the build is durably
    # committed, so a failing dataset is still there to inspect afterward.
    with db.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT dataset_id FROM current_dataset WHERE id")
            row = cursor.fetchone()
        previous_dataset_id = row[0] if row else None
        result = validation.validate_dataset(connection, dataset_id, previous_dataset_id)
        status = "validated" if result.ok else "failed"
        notes = "; ".join(result.errors) or None
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dataset_versions SET status = %s, finished_at = now(), notes = %s WHERE dataset_id = %s",
                (status, notes, dataset_id),
            )
        connection.commit()

    for warning in result.warnings:
        log.warning("validation warning: %s", warning)
    if result.ok:
        log.info("dataset_id=%s validated successfully; run scripts/publish_dataset.py to go live", dataset_id)
    else:
        log.error("dataset_id=%s FAILED validation and was NOT published:", dataset_id)
        for error in result.errors:
            log.error("  - %s", error)
    return dataset_id, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--scraper-git-sha", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    dataset_id, result = import_dataset(data_dir=args.data_dir, scraper_git_sha=args.scraper_git_sha)
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
