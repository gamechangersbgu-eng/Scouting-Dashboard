"""Add persistent, user-owned shortlist application state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30

This intentionally does not reference ``dataset_versions``.  A shortlist is
created by a coach, not by an importer, and must survive dataset refresh,
publication, and rollback.  ``shortlist_players`` stores an identity snapshot
instead of a foreign key to ``players`` because CSV mode can use the same
PostgreSQL user-state database before a canonical dataset has been imported.
The application validates every new player against its loaded catalog.
"""
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # ``users`` is provisioned by dashboard.manage_users before migrations;
    # user ownership is application state, whereas 0001--0003 are canonical
    # scouting-data schema migrations.
    op.execute(
        """
        CREATE TABLE shortlists (
            id bigserial PRIMARY KEY,
            user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name varchar(80) NOT NULL,
            is_default boolean NOT NULL DEFAULT FALSE,
            created_at timestamptz NOT NULL DEFAULT NOW(),
            updated_at timestamptz NOT NULL DEFAULT NOW(),
            CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
            CHECK ((is_default AND name = 'מועדפים') OR (NOT is_default AND name <> 'מועדפים')),
            UNIQUE (user_id, name)
        )
        """
    )
    # This is the concurrency-safe guard for lazy Favorites creation.
    op.execute(
        "CREATE UNIQUE INDEX uq_shortlists_one_default_per_user ON shortlists (user_id) WHERE is_default"
    )
    op.execute("CREATE INDEX ix_shortlists_user_created ON shortlists (user_id, created_at, id)")
    op.execute(
        """
        CREATE TABLE shortlist_players (
            shortlist_id bigint NOT NULL REFERENCES shortlists(id) ON DELETE CASCADE,
            player_id text NOT NULL,
            -- Snapshots are only a last-known fallback when a later current
            -- catalog no longer contains this player; they are not live stats.
            player_name text,
            birth_year integer,
            last_known_team text,
            created_at timestamptz NOT NULL DEFAULT NOW(),
            PRIMARY KEY (shortlist_id, player_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_shortlist_players_player ON shortlist_players (player_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS shortlist_players")
    op.execute("DROP TABLE IF EXISTS shortlists")
