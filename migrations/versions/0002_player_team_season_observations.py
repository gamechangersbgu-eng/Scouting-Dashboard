"""Lossless per-source observation layer, restoring source-membership parity.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

Problem this fixes
-------------------
``player_team_seasons`` (0001) is the canonical *merged* fact table: at most
one row per ``(dataset_id, player_id, team_id, season_id)``, with
``source_file`` recording only which source's stats *won* the importer's
merge (``dashboard.app_core.merge_canonical_rows``). That is exactly right
for canonical/merged reads, but it silently discards the *other* source's
row whenever both sources publish the same (player, team, season) key --
which the real data does 49,379 times out of 49,593 recent rows. A reader
that tries to reconstruct "every row CsvScoutingData's ``player_season_stats.csv``
would show" by filtering ``player_team_seasons`` on
``source_file = 'player_season_stats'`` therefore gets back only the 214 rows
that happened to *win* their merge, not the 49,593 rows the CSV actually has
-- source-membership information the merge legitimately needs to discard for
the canonical table is information a faithful CSV-equivalent *read path*
still needs.

Fix: record every source's row before the merge ever runs, at a grain that
includes ``source_file``, so both sources' full row sets remain queryable
independently of which one won the canonical merge.

Grain and columns
------------------
``(dataset_id, player_id, team_id, season_id, source_file)`` -- one row per
*observation*, i.e. per (player, team, season) as published by one specific
CSV source. Unlike ``player_team_seasons``, ``source_file`` is part of the
key here on purpose: this table's entire job is to keep the two sources'
observations of a shared key apart.

The display/stat columns mirror ``player_team_seasons`` (player_name,
team_name, age_group, league_name, the nine numeric stat fields,
stats_available/stats_source/stats_completeness) with one deliberate
difference: ``stats_available``, ``stats_source`` and ``stats_completeness``
are nullable here, not ``NOT NULL``. ``player_season_stats.csv`` genuinely
does not have those columns at all (confirmed against this repository's own
``data/player_season_stats.csv`` header), so a lossless observation of one of
its rows must record "this source did not publish a value" as SQL NULL --
exactly what ``pandas.read_csv`` already does when a CSV lacks a column, and
exactly what ``dashboard.app_core.BaseScoutingData._normalise_stat_rows``
already expects to fill in a computed default for at read time. Storing a
coerced ``False``/``'unavailable'`` here instead (the way the importer's
merge-time helpers do for the canonical table, where "unknown" is not a
representable state) would corrupt that inference for every recent-source
observation that has real games/goals/minutes but simply predates the
stats-metadata columns.

``league_id`` (the CSV's own raw, sometimes comma-joined display column) is
deliberately NOT duplicated onto this table. ``player_team_seasons``
doesn't carry it either (see 0001's "Why this grain, and not source_file or
league_id"); this table only reproduces the fields
``dashboard.postgres_source.PostgresScoutingData`` needs to rebuild the two
source-specific row populations, and league_id is already reconstructed at
read time from the normalized ``team_season_leagues`` junction -- unaffected
by this migration.

Not dataset-versioned rollback risk: like ``player_team_seasons``, this table
is keyed by ``dataset_id``, so rolling ``current_dataset`` back to an older
dataset's observations is exactly as reproducible (or not) as its canonical
facts already are -- see 0001's "Rollback reproducibility".
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE player_team_season_observations (
            id                       bigserial PRIMARY KEY,
            dataset_id               bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            player_id                text NOT NULL,
            team_id                  text NOT NULL,
            season_id                integer NOT NULL,
            source_file              text NOT NULL CHECK (source_file IN ('player_season_stats', 'player_history')),
            player_name              text,
            team_name                text,
            age_group                text,
            league_name              text,
            games                    integer CHECK (games IS NULL OR games >= 0),
            goals                    integer CHECK (goals IS NULL OR goals >= 0),
            minutes                  integer CHECK (minutes IS NULL OR minutes >= 0),
            starts                   integer CHECK (starts IS NULL OR starts >= 0),
            sub_on                   integer CHECK (sub_on IS NULL OR sub_on >= 0),
            sub_off                  integer CHECK (sub_off IS NULL OR sub_off >= 0),
            yellow_cards_league_cup  integer CHECK (yellow_cards_league_cup IS NULL OR yellow_cards_league_cup >= 0),
            yellow_cards_toto        integer CHECK (yellow_cards_toto IS NULL OR yellow_cards_toto >= 0),
            red_cards                integer CHECK (red_cards IS NULL OR red_cards >= 0),
            -- Nullable, unlike player_team_seasons: NULL means "this source did
            -- not publish this column at all" (see module docstring), a state
            -- the canonical table's NOT NULL columns cannot represent.
            stats_available          boolean,
            stats_source             text,
            stats_completeness       text CHECK (stats_completeness IS NULL OR stats_completeness IN ('full', 'partial', 'unavailable')),
            -- source_file is part of the grain here -- the opposite choice from
            -- player_team_seasons -- because keeping both sources' observations
            -- of a shared (player, team, season) apart is this table's entire
            -- purpose. _load_season_source() already de-duplicates each CSV to
            -- at most one row per natural key before the importer ever gets
            -- here, so this constraint should never reject a real import.
            UNIQUE (dataset_id, player_id, team_id, season_id, source_file)
        )
        """
    )
    # Primary read pattern: PostgresScoutingData._read_season_rows() fetches one
    # source's full row set for the live dataset.
    op.execute(
        "CREATE INDEX ix_ptso_dataset_source ON player_team_season_observations (dataset_id, source_file)"
    )
    op.execute(
        "CREATE INDEX ix_ptso_dataset_player ON player_team_season_observations (dataset_id, player_id)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS player_team_season_observations")
