"""Read-only Postgres-backed data source for the dashboard.

Loaded lazily by ``app_core._resolve_data_source`` only when
``DATA_SOURCE=postgres``, so psycopg does not need to be installed for the
default CSV path to work at all.

This reads whichever dataset ``current_dataset`` currently points at -- never
a fixed dataset_id -- so restarting the web process always serves the latest
published (or rolled-back-to) dataset. The web process itself never writes to
the canonical tables: publication and rollback are separate, operator-run
scripts (``scripts/publish_dataset.py`` / ``scripts/rollback_dataset.py``),
and in production this class is expected to connect with the read-only
``app_reader`` database role.

Two tables in the schema are deliberately *not* dataset-versioned:
``players`` and ``teams``/``venues`` are slowly-changing dimension tables --
one row per player/team, holding the latest known name, birth year, image
and home venue -- upserted on every import regardless of which dataset is
published. See the canonical schema migration's "Rollback reproducibility"
section for exactly what that does and does not affect (short version: the
player identity name shown here is derived from this dataset's own fact
rows, not the dimension row, so it is not actually a rollback risk;
birth_year/image_url are treated as "always show latest known" reference
data, matching the CSV pipeline's existing behavior).

``teams``/``venues`` are NOT what ``_load_locations()`` reads, though --
``venues`` is deduplicated by field_id (multiple team_ids can share one
physical field, and ``team_locations.csv`` genuinely geocodes some of them to
different cities -- see the 0003 migration's docstring), which is not how
``CsvScoutingData._load_locations()`` reads ``team_locations.csv`` at all (it
is keyed by team_id, verbatim, no dedup). ``_load_locations()`` below reads
the dataset-versioned ``dataset_team_locations`` table instead -- one row per
team_id, snapshotted from ``team_locations.csv`` at import time -- which also
means a dataset rollback's location data is reproducible: it can no longer
change just because ``team_locations.csv`` was re-geocoded after that dataset
was imported (the same rollback-reproducibility property 0001 already gives
``player_team_seasons``).

``player_team_seasons`` is one row per (player_id, team_id, season_id) --
never per source_file, which is provenance only (which CSV source's stats
won the importer's merge). It is the canonical/merged view and this module
deliberately does NOT read it for ``_load_season_rows``/``_load_history_rows``/
``_load_players``: those three need every row each source published,
including the ones the merge discarded, to reproduce
``player_season_stats.csv``/``player_history.csv`` faithfully. That full,
unmerged row population lives in ``player_team_season_observations`` (see
the 0002 migration) instead -- one row per (player_id, team_id, season_id,
source_file), written by the importer before it ever merges anything.

Which league(s) a (team_id, season_id) played in is a separate, normalized
fact in ``team_season_leagues`` (one row per league, real foreign key to
``leagues``) rather than a column on either fact table, because the source
stats themselves are not decomposable per league -- see the 0001 migration's
"Why this grain, and not source_file or league_id" for the real data that
established this. The season-row queries below reconstruct a comma-joined
``league_id`` display string from that join purely so
``BaseScoutingData.player()``'s existing season-display code (shared with
the CSV path, which still has a genuine comma-joined ``league_id`` column)
does not need a Postgres-specific branch.
"""

import logging
import warnings

import pandas as pd

from ifa_scraper import db, run

from .app_core import BaseScoutingData

log = logging.getLogger(__name__)

# Reads player_team_season_observations, NOT player_team_seasons: the
# canonical table keeps at most one row per (player, team, season) after the
# importer's merge, so filtering it by source_file only returns whichever
# source happened to *win* that merge -- 214 of 49,593 recent rows in this
# repository's own data, not the CSV's full row population. The observation
# table records every source's row before any merge happens (see the 0002
# migration), which is what a faithful CSV-equivalent read path needs. See
# the 0002 migration's docstring for the full "why".
#
# ORDER BY o.source_row_number is not cosmetic: BaseScoutingData.player() does
# a *stable* sort by (season_id, team_name), and two rows genuinely can tie on
# both (two team_ids sharing the exact same team_name in the same season --
# confirmed in this repository's own data, see the 0003 migration's
# docstring) -- the stable sort then falls through to whatever order the rows
# arrived in. CsvScoutingData gets that from pandas.read_csv's file order;
# without this ORDER BY, a plain SQL SELECT makes no such promise at all, so
# ties could resolve to a different "first club" than the CSV picks.
_SEASON_ROW_SQL = """
    SELECT
        o.player_id,
        o.player_name,
        o.season_id,
        s.label AS season,
        o.team_id,
        o.team_name,
        COALESCE(tsl.league_ids, '') AS league_id,
        o.age_group,
        o.league_name,
        o.games,
        o.goals,
        o.minutes,
        o.starts,
        o.sub_on,
        o.sub_off,
        o.yellow_cards_league_cup,
        o.yellow_cards_toto,
        o.red_cards,
        o.stats_available,
        o.stats_source,
        o.stats_completeness
    FROM player_team_season_observations o
    JOIN seasons s ON s.season_id = o.season_id
    LEFT JOIN (
        SELECT dataset_id, team_id, season_id,
               string_agg(league_id::text, ', ' ORDER BY league_id) AS league_ids
        FROM team_season_leagues
        GROUP BY dataset_id, team_id, season_id
    ) tsl ON tsl.dataset_id = o.dataset_id
         AND tsl.team_id = o.team_id
         AND tsl.season_id = o.season_id
    WHERE o.dataset_id = %(dataset_id)s AND o.source_file = %(source_file)s
    ORDER BY o.source_row_number
"""


class PostgresScoutingData(BaseScoutingData):
    """Loads the canonical view from the live dataset or an explicit admin override."""

    def __init__(self, database_url=None, dataset_id=None):
        self.database_url = db.resolve_database_url(database_url)
        if dataset_id is None:
            with db.connect(self.database_url) as connection:
                self.dataset_id = db.current_dataset_id(connection)
        else:
            self.dataset_id = dataset_id
        if self.dataset_id is None:
            raise RuntimeError(
                "no dataset is currently published (current_dataset is empty); "
                "run scripts/publish_dataset.py before starting with "
                "DATA_SOURCE=postgres"
            )
        log.info("PostgresScoutingData starting on dataset_id=%s", self.dataset_id)
        super().__init__()

    def _read_season_rows(self, source_file):
        """Fetch one source's player-team-season rows via pandas, like the CSV path.

        psycopg3 connections are plain DBAPI2 objects rather than SQLAlchemy
        connectables, so pandas falls back to a slower code path and warns
        about it on every call; the warning is expected here and silenced
        rather than logged as if something were wrong.
        """
        with db.connect(self.database_url) as connection:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                frame = pd.read_sql(
                    _SEASON_ROW_SQL,
                    connection,
                    params={"dataset_id": self.dataset_id, "source_file": source_file},
                )
        return frame

    def _load_players(self):
        # Reproduce players_youth.csv exactly: it is built by aggregate_players()
        # over the recent scrape's own season rows only (ifa_scraper.run.main --
        # phase2_collect_squads output, i.e. player_season_stats.csv), never over
        # player_history.csv. Aggregating history rows in here too would surface
        # historical age groups/leagues/current_team as if they were part of the
        # current catalog -- exactly the parity failures the merged-canonical-rows
        # version of this method caused (see the 0002 migration's docstring for
        # the full story); the cross-source known-name recovery that
        # player_history.csv *does* contribute happens later, in player() itself.
        #
        # ORDER BY o.source_row_number below matters here too:
        # aggregate_players() does its own stable sort by (-season_id,
        # team_name), so a player's current_team can depend on row order the
        # same way player()'s first-club tie-break does -- see the 0003
        # migration's docstring.
        #
        # Deliberately not routed through _normalise_stat_rows/pandas:
        # aggregate_players() expects plain Python None/int/str values (it does
        # `row[field] or 0` and calls .split(", ") on age_group/league_name
        # unconditionally), and pandas' nullable Int64 dtype uses pd.NA, whose
        # truthiness is ambiguous and would break that unconditional `or 0`. A
        # raw cursor keeps this the same shape aggregate_players already expects
        # from the scraper's own season rows.
        with db.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        o.player_id, o.player_name, o.season_id, s.label,
                        o.team_id, o.team_name, o.age_group,
                        o.league_name, o.games, o.goals, o.minutes, o.starts,
                        o.sub_on, o.sub_off, o.yellow_cards_league_cup,
                        o.yellow_cards_toto, o.red_cards
                    FROM player_team_season_observations o
                    JOIN seasons s ON s.season_id = o.season_id
                    WHERE o.dataset_id = %s AND o.source_file = 'player_season_stats'
                    ORDER BY o.source_row_number
                    """,
                    (self.dataset_id,),
                )
                # aggregate_players() never reads league_id (see run.aggregate_players --
                # it only uses age_group/league_name and the numeric stat fields), so this
                # catalog-building query does not need the team_season_leagues join at all.
                columns = [
                    "player_id", "player_name", "season_id", "season", "team_id",
                    "team_name", "age_group", "league_name", "games",
                    "goals", "minutes", "starts", "sub_on", "sub_off",
                    "yellow_cards_league_cup", "yellow_cards_toto", "red_cards",
                ]
                season_rows = [
                    {
                        **dict(zip(columns, row)),
                        # aggregate_players() splits these on ", " unconditionally.
                        "age_group": row[6] or "",
                        "league_name": row[7] or "",
                    }
                    for row in cursor.fetchall()
                ]

                cursor.execute("SELECT player_id, birth_year, image_url FROM players")
                details = {
                    player_id: {"birth_year": birth_year, "image_url": image_url or ""}
                    for player_id, birth_year, image_url in cursor.fetchall()
                }

        players = run.aggregate_players(season_rows, splits={}, details=details)
        log.info(
            "loaded %s players (dataset_id=%s)", len(players), self.dataset_id
        )
        return {p["player_id"]: p for p in players}

    def _load_season_rows(self):
        frame = self._read_season_rows("player_season_stats")
        log.info("loaded %s player-season rows (dataset_id=%s)", len(frame), self.dataset_id)
        return self._normalise_stat_rows(
            frame, source_name="player_team_season_observations(player_season_stats)"
        )

    def _load_history_rows(self):
        frame = self._read_season_rows("player_history")
        if frame.empty:
            return []
        # Mirrors CsvScoutingData._load_history_rows(): the full historical export
        # covers players outside the current recent-scrape catalog too, and they
        # cannot be opened through this dashboard, so they are dropped here rather
        # than carried into memory for every request.
        frame = frame[frame["player_id"].isin(self.players)].copy()
        log.info(
            "loaded %s historical player-team-season rows (dataset_id=%s)",
            len(frame),
            self.dataset_id,
        )
        return self._normalise_stat_rows(
            frame, source_name="player_team_season_observations(player_history)"
        )

    def _load_locations(self):
        # Reads dataset_team_locations, NOT teams/venues: those two are kept
        # as latest-known dimensions (see the module docstring above) and
        # venues is deduplicated by field_id, which silently discards a real,
        # distinct team_locations.csv city for every team_id that shares a
        # field with some other team -- confirmed in this repository's own
        # data (field_id 48 alone is home to 11 different team_ids that don't
        # all geocode to the same city). CsvScoutingData._load_locations()
        # never goes through any field_id dedup either -- it reads
        # team_locations.csv straight, keyed by team_id -- so
        # dataset_team_locations (see the 0003 migration) is the only way to
        # reproduce that per-team city for _career_track()/_infer_likely_origin().
        with db.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT team_id, field_name, city, address, lat, lon, precision
                    FROM dataset_team_locations
                    WHERE dataset_id = %s
                    """,
                    (self.dataset_id,),
                )
                columns = ["team_id", "field_name", "city", "address", "lat", "lon", "precision"]
                return {row[0]: dict(zip(columns, row)) for row in cursor.fetchall()}

    def _load_details(self):
        with db.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT player_id, birth_year FROM players")
                return {
                    player_id: {"birth_year": birth_year}
                    for player_id, birth_year in cursor.fetchall()
                }
