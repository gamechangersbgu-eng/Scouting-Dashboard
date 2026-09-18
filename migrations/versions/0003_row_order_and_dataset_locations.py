"""Preserve source row order and per-team location data lost by 0001/0002.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

Dataset 5 parity audit (1,002 players compared) found 13 remaining
mismatches across 9 players, all in ``first_club_city``/``likely_origin_city``.
Two distinct, unrelated causes, both confirmed against this repository's own
``data/`` files before writing this migration.

CAUSE A -- first-club tie ordering depends on unordered SQL reads
--------------------------------------------------------------------
``BaseScoutingData.player()`` sorts a player's rows by
``(season_id, team_name)`` (a stable sort), then ``_career_track()`` groups
them into club spells and sorts those by ``first_season_id`` (also stable).
``first_club = teams[0]``.

When two spells tie on *both* keys -- which happens for real, not as an edge
case: player_id 160443's two season-16 rows (team_id 6166 and 6351) have the
byte-for-byte identical ``team_name`` "הפ' בני אעבלין"; player_id 227243's
season-22 rows (team_id 2117, 2336) both say "הפועל חיפה 1"; player_id
206511's season-20 rows include two team_ids (1176, 1629) both named "מכבי
חיפה גולדשנפלד" -- Python's stable sort falls through to whatever order the
rows arrived in. For ``CsvScoutingData`` that is ``pandas.read_csv``'s row
order, i.e. ``player_history.csv``'s own line order, which is exactly what a
human reading the CSV would call "the first one listed". For
``PostgresScoutingData`` that was ``player_team_season_observations``'s
default (SQL does not guarantee any row order without ``ORDER BY``, and this
table's own ``SELECT`` never adds one) -- so ties resolved however Postgres's
query planner happened to return rows, unrelated to the CSV's real order.

Fix: a new ``source_row_number`` column on ``player_team_season_observations``,
populated by the importer from each row's position in its own source CSV
(post de-duplication, so it reflects the row that actually survived), and
``dashboard.postgres_source._SEASON_ROW_SQL`` now orders by it explicitly.
Deliberately NOT relying on ``id`` (bigserial, insertion order) even though
``COPY`` happens to preserve input order today -- that's an implementation
detail of one INSERT path, not a documented ordering guarantee, and the next
importer change (a retry loop, a partial re-COPY, anything that inserts out
of file order) would silently break it again with no test able to catch it.
An explicit, importer-assigned row number is the only version of "row order"
this schema can promise to keep meaning the same thing across importer
changes.

CAUSE B -- team locations are deduplicated by field_id, not kept per team_id
------------------------------------------------------------------------------
``CsvScoutingData._load_locations()`` reads ``team_locations.csv`` straight,
keyed by ``team_id`` -- one geocoded row per team, verbatim.
``scripts.import_canonical_dataset._load_venue_sources()`` instead builds
``venues_by_field`` keyed by ``field_id``, keeping only the *first*
``team_locations.csv`` row seen for a given field_id and discarding the rest;
``PostgresScoutingData._load_locations()`` then joins
``teams.current_field_id -> venues``, so every team sharing a field_id with
some other team inherits whichever team happened to be first in the CSV for
that field_id.

This is not a hypothetical: field_id 48 ("מג'ד אל כרום") is the home ground
of 11 different team_ids in this repository's own ``data/team_fields.csv``.
team_locations.csv geocodes most of them to city "מג'ד אל כרום" (a
field-name fallback geocode), but team_id 6480 -- home of player_id 291208
and 302022's earliest club -- geocodes on its own team_name to a
more-specific, different locality: "מגדל". The field_id-keyed importer
collapses that down to whichever team happened to load first, discarding
6480's own, more accurate row entirely. Likewise field_id 1311 (players
268421/284978) is shared by "הפועל בועיינה" (city "בועיינה") and "מכבי
נוג'ידאת אחמד" (city "נוג'ידאת") -- two real, distinct localities sharing one
physical field, not a data error.

``teams``/``venues`` stay exactly as they are -- 0001's own "latest known"
dimension design is still the right call for "where does this team play
today" (a map marker, say). They were just never the right source for
*historical* per-team-per-dataset location semantics, which
``CsvScoutingData`` never went through them for either.

Fix: a new dataset-versioned table, ``dataset_team_locations``, grain
``(dataset_id, team_id)`` -- one row per ``team_locations.csv`` line
(confirmed unique on team_id in this repository's own file: 2,774 rows,
2,774 distinct team_ids), populated directly from that CSV by the importer,
never deduplicated by field_id. ``PostgresScoutingData._load_locations()``
now reads this table instead of the ``teams``/``venues`` join. This also
makes a dataset rollback's location data reproducible the way 0001 already
made ``player_team_seasons`` reproducible -- see that migration's "Rollback
reproducibility": current-venue metadata living only in the unversioned
``teams``/``venues`` tables could previously change an *older* published
dataset's ``first_club_city``/origin inference after a rollback, purely
because ``team_locations.csv`` had since been re-geocoded. Now it can't --
each dataset keeps its own snapshot.

Neither fix modifies ``player_team_seasons`` or ``player_team_season_observations``
rows already written for datasets 4/5 -- ``source_row_number`` is added as a
nullable column (existing rows simply have no row-order opinion until
re-imported, same as dataset 4's pre-0002 rows had no observations at all
until re-imported), and ``dataset_team_locations`` is an additive table with
no rows for any dataset until a new import populates it.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # --- Cause A: explicit, importer-assigned row order -------------------
    op.execute(
        """
        ALTER TABLE player_team_season_observations
        ADD COLUMN source_row_number integer CHECK (source_row_number IS NULL OR source_row_number >= 0)
        """
    )
    # Supports "WHERE dataset_id = ... AND source_file = ... ORDER BY
    # source_row_number" -- PostgresScoutingData's primary read query --
    # without a separate sort step.
    op.execute(
        """
        CREATE INDEX ix_ptso_dataset_source_order
        ON player_team_season_observations (dataset_id, source_file, source_row_number)
        """
    )

    # --- Cause B: per-team_id, dataset-versioned location snapshot --------
    op.execute(
        """
        CREATE TABLE dataset_team_locations (
            dataset_id  bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            team_id     text NOT NULL,
            field_id    text,
            field_name  text,
            city        text,
            address     text,
            lat         double precision,
            lon         double precision,
            precision   text,
            PRIMARY KEY (dataset_id, team_id)
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS dataset_team_locations")
    op.execute("DROP INDEX IF EXISTS ix_ptso_dataset_source_order")
    op.execute("ALTER TABLE player_team_season_observations DROP COLUMN IF EXISTS source_row_number")
