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
birth_year/image_url and a team's current venue are treated as "always show
latest known" reference data, matching the CSV pipeline's existing venue
behavior and IFA's own single-value team-venue data).

``player_team_seasons`` is one row per (player_id, team_id, season_id) --
never per source_file, which is provenance only (which CSV source's stats
won the importer's merge). Which league(s) a (team_id, season_id) played in
is a separate, normalized fact in ``team_season_leagues`` (one row per
league, real foreign key to ``leagues``) rather than a column on this table,
because the source stats themselves are not decomposable per league -- see
the migration's "Why this grain, and not source_file or league_id" for the
real data that established this. The season-row queries below reconstruct a
comma-joined ``league_id`` display string from that join purely so
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

_SEASON_ROW_SQL = """
    SELECT
        pts.player_id,
        pts.player_name,
        pts.season_id,
        s.label AS season,
        pts.team_id,
        pts.team_name,
        COALESCE(tsl.league_ids, '') AS league_id,
        pts.age_group,
        pts.league_name,
        pts.games,
        pts.goals,
        pts.minutes,
        pts.starts,
        pts.sub_on,
        pts.sub_off,
        pts.yellow_cards_league_cup,
        pts.yellow_cards_toto,
        pts.red_cards,
        pts.stats_available,
        pts.stats_source,
        pts.stats_completeness
    FROM player_team_seasons pts
    JOIN seasons s ON s.season_id = pts.season_id
    LEFT JOIN (
        SELECT dataset_id, team_id, season_id,
               string_agg(league_id::text, ', ' ORDER BY league_id) AS league_ids
        FROM team_season_leagues
        GROUP BY dataset_id, team_id, season_id
    ) tsl ON tsl.dataset_id = pts.dataset_id
         AND tsl.team_id = pts.team_id
         AND tsl.season_id = pts.season_id
    WHERE pts.dataset_id = %(dataset_id)s AND pts.source_file = %(source_file)s
"""


class PostgresScoutingData(BaseScoutingData):
    """Loads the canonical view from the currently-published Postgres dataset."""

    def __init__(self, database_url=None):
        self.database_url = db.resolve_database_url(database_url)
        with db.connect(self.database_url) as connection:
            self.dataset_id = db.current_dataset_id(connection)
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
        # Deliberately not routed through _normalise_stat_rows/pandas: aggregate_players()
        # expects plain Python None/int/str values (it does `row[field] or 0` and calls
        # .split(", ") on age_group/league_name unconditionally), and pandas' nullable
        # Int64 dtype uses pd.NA, whose truthiness is ambiguous and would break that
        # unconditional `or 0`. A raw cursor keeps this the same shape aggregate_players
        # already expects from the scraper's own season rows.
        with db.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        pts.player_id, pts.player_name, pts.season_id, s.label,
                        pts.team_id, pts.team_name, pts.age_group,
                        pts.league_name, pts.games, pts.goals, pts.minutes, pts.starts,
                        pts.sub_on, pts.sub_off, pts.yellow_cards_league_cup,
                        pts.yellow_cards_toto, pts.red_cards
                    FROM player_team_seasons pts
                    JOIN seasons s ON s.season_id = pts.season_id
                    WHERE pts.dataset_id = %s AND pts.source_file = 'player_season_stats'
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
        return self._normalise_stat_rows(frame, source_name="player_team_seasons(player_season_stats)")

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
        return self._normalise_stat_rows(frame, source_name="player_team_seasons(player_history)")

    def _load_locations(self):
        with db.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT t.team_id, v.field_name, v.city, v.address, v.lat, v.lon, v.precision
                    FROM teams t
                    JOIN venues v ON v.field_id = t.current_field_id
                    """
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
