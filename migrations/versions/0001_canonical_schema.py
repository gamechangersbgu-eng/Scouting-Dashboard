"""Canonical scouting-data schema: dataset-versioned facts over fixed dimensions.

Revision ID: 0001
Revises:
Create Date: 2026-09-17

This is the corrected design from the architecture self-audit, not the
originally-proposed schema-rename-swap:

* ``dataset_versions`` + ``current_dataset`` (a one-row singleton, enforced by
  the ``id boolean PRIMARY KEY DEFAULT true CHECK (id)`` trick) hold which
  dataset is live. Publishing a new dataset is one small transaction that
  flips ``current_dataset.dataset_id`` -- it never renames or drops a schema,
  so ``scrape_runs``-equivalent bookkeeping (here, ``dataset_versions``
  itself) is never destroyed by publication the way a rename-swap would.
* ``players`` / ``teams`` / ``leagues`` / ``venues`` / ``seasons`` are
  slowly-changing *dimension* tables: one row per identity, upserted on every
  import, and NOT dataset-versioned. See "Rollback reproducibility" below for
  exactly what that does and does not mean for what rollback reproduces.
* ``player_team_seasons`` is the one dataset-versioned *fact* table so far.
  Its grain is ``(dataset_id, player_id, team_id, season_id)`` -- see "Why
  this grain, and not source_file or league_id" below for how that was
  determined from the actual scraper and CSV data, not assumed.
* ``team_season_leagues`` is a normalized many-to-many fact: which league_ids
  a (team_id, season_id) was discovered under, with a real foreign key to
  ``leagues`` -- one row per league, never a comma-joined value.
* ``player_origin_estimates`` is the first example of dataset-versioned
  *derived* data, kept in its own table (never merged into
  ``player_team_seasons``) and keyed by ``(dataset_id, player_id,
  algorithm_version)`` so a new inference algorithm never silently overwrites
  the only copy of a previous run's results, and every estimate says which
  dataset and which algorithm version produced it.
* Match/lineup/substitution/position-prediction tables are intentionally NOT
  part of this migration -- only the architecture (dataset_id threading,
  fact/dimension/derived separation) is meant to support adding them later.

Why this grain, and not source_file or league_id
--------------------------------------------------
An earlier version of this migration used
``UNIQUE (dataset_id, player_id, team_id, season_id, source_file)`` and put a
comma-joined ``league_id_raw`` text column on every row. Both were wrong, for
two different, demonstrated reasons:

1. ``source_file`` (which CSV a row came from: ``player_season_stats`` vs
   ``player_history``) is provenance, not football identity -- two sources
   observing the same (player, team, season) does not mean two different
   football facts exist. The importer now merges them into one row per grain
   *before* insert, using ``dashboard.app_core.merge_canonical_rows`` -- the
   exact same stats-quality-ranking and masked-name-safe algorithm
   ``BaseScoutingData.player()`` already used to merge the two sources at
   read time for the CSV path, extracted into one shared function so the two
   call sites cannot drift apart. ``source_file`` stays as a plain column on
   the winning row (which source's stats won), never part of the key.

2. league_id genuinely cannot be part of this table's grain, because IFA's
   own ``team_player_stats`` endpoint -- what ``ifa_scraper.run.
   phase2_collect_squads`` calls to get games/goals/minutes/etc. -- takes
   only ``(team_id, season_id)``, not a league_id, and returns one already
   -aggregated stats line for that team-season. There is no way to recover
   "how many of those games were in league X vs league Y" from the source
   data at all: it was never broken out by league to begin with. A team-
   season is discovered as playing in more than one league when
   ``phase1_collect_teams`` finds the same team_id under more than one
   configured/discovered league listing for that season -- confirmed against
   the real data in this repository: scanning ``data/player_history.csv``
   found 153 rows (of 194,518) with a multi-valued league_id, e.g. player_id
   151674, team_id 5613, season_id 16 (2014/15), league_id "182, 740",
   league_name "טורנירים טרום ילדים ב' מרכז, ליגה ילדים טרום א' דן" (two
   distinct competitions, one an explicit "טורניר"/tournament), age_group
   "ילדים טרום א, ילדים טרום ב" (two distinct age brackets), games=14 as one
   single number. Splitting that one row into two -- one per league_id --
   would either double-count those 14 games or require assigning them to one
   league arbitrarily; both are fabrication. So games/goals/minutes/etc.
   belong on one row per (player, team, season), full stop, and which
   leagues that team-season spanned is recorded separately, in
   ``team_season_leagues``, where it is decomposable because no stats are
   attached to it.

   (This also uncovered a real pairing bug, now fixed in
   ``ifa_scraper.run.phase1_collect_teams``/``phase2_collect_squads``: the
   old code built ``league_ids`` and ``league_names`` as two independently-
   sorted sets -- numeric order for ids, string order for names -- so a
   team-season's Nth league_id was not guaranteed to be the Nth league_name.
   The fix keeps league_id, league_name and age_group together as one
   ordered structure per team-season and derives every display string from
   the same order. ``player_team_seasons.age_group``/``league_name`` remain
   plain display text on the row -- unchanged in kind from the CSV columns
   they mirror -- while ``team_season_leagues`` is the actual place to query
   "which leagues" relationally.)

Rollback reproducibility
-------------------------
Rolling back ``current_dataset`` restores which *player_team_seasons rows*
(and therefore which stats, and which per-season display text -- team_name,
age_group, league_name, player_name are all columns on that dataset-
versioned row) are live. It does not roll back the dimension tables
(``players.latest_name``/``birth_year``/``image_url``, ``teams.latest_name``,
``teams.current_field_id``/``venues``) to what they were as of the older
dataset's own import, because those are upserted in place on every import,
independent of dataset_id.

Checked what that actually means for what a rolled-back dataset renders:

* Player identity name: NOT a rollback risk in practice.
  ``dashboard.app_core.BaseScoutingData.player()`` derives the player_name it
  returns from that player's *own dataset-versioned fact rows*
  (``merge_canonical_rows`` over the live dataset's rows, ranked via
  ``name_quality.best_name``), falling back to the ``players`` dimension row
  only when a player has no fact rows at all. So the identity name shown on
  a player's page is already dataset-scoped and reproduces correctly after
  rollback; ``players.latest_name``/``name_status`` are written by the
  importer (so a future consumer can rely on them) but are not currently
  read by the dashboard at all.
* Player birth_year / image_url: read from the unversioned ``players``
  dimension row. These are treated as reference data that should always
  reflect the best currently-known value regardless of which stats dataset
  is live (a birth year discovered after the fact is a correction, not a
  new fact that should disappear on rollback) -- the same trade-off already
  documented above for dimension tables in general. Low risk in practice:
  birth_year is essentially immutable once known, and image_url changing is
  cosmetic.
* Team current venue (``teams.current_field_id`` / ``venues``): read the
  same way for every spell in a player's career, not just their current
  team -- e.g. a player's very first, decade-old club is shown at that
  club's *current* home ground, never a historical one. This is not a
  rollback bug: it is unchanged, identical behavior to the CSV pipeline
  (``data/team_locations.csv`` also only ever holds each team's current
  venue), and the user's own instructions for this task were explicit --
  do not invent historical venue data that does not exist. A genuine fix
  would require IFA to publish when a team's home ground changed, which it
  does not; nothing here attempts to fabricate that.

Net: the smallest clean thing that mattered here was already true before
this review (the player_name fix from the masked-name work happens to also
make player identity dataset-scoped), so no schema change was needed for
rollback reproducibility specifically -- the two remaining exceptions
(birth_year/image_url, venue) are both intentional, documented, low-risk
"latest known" reference-data trade-offs rather than gaps to close now.

This DDL was verified interactively against a real PostgreSQL 16 instance
before being wrapped here: table creation, the unique-key rejection of a
duplicate (dataset_id, player_id, team_id, season_id), the
publish/rollback transaction shapes, and the validation queries all behaved
as expected. See the implementation report for that transcript. Running this
migration itself through Alembic against a real database is still an
operator step this task could not perform (no DATABASE_URL / no network-
installable psycopg or alembic in this environment).
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE seasons (
            season_id   integer PRIMARY KEY,
            label       text NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE players (
            player_id     text PRIMARY KEY,
            latest_name   text NOT NULL,
            name_status   text NOT NULL CHECK (name_status IN ('known', 'masked', 'missing')),
            birth_year    integer,
            image_url     text
        )
        """
    )

    op.execute(
        """
        CREATE TABLE venues (
            field_id    text PRIMARY KEY,
            field_name  text,
            city        text,
            address     text,
            lat         double precision,
            lon         double precision,
            precision   text
        )
        """
    )

    op.execute(
        """
        CREATE TABLE teams (
            team_id            text PRIMARY KEY,
            latest_name        text NOT NULL,
            current_field_id   text REFERENCES venues(field_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE leagues (
            league_id     integer PRIMARY KEY,
            latest_name   text NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE dataset_versions (
            dataset_id       bigserial PRIMARY KEY,
            started_at       timestamptz NOT NULL DEFAULT now(),
            finished_at      timestamptz,
            status           text NOT NULL CHECK (status IN ('building', 'validated', 'failed', 'live')),
            scraper_git_sha  text,
            cache_hits       integer,
            cache_fetches    integer,
            row_counts       jsonb,
            notes            text
        )
        """
    )

    op.execute(
        """
        CREATE TABLE current_dataset (
            id          boolean PRIMARY KEY DEFAULT true CHECK (id),
            dataset_id  bigint NOT NULL REFERENCES dataset_versions(dataset_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE dataset_publication_log (
            id           bigserial PRIMARY KEY,
            dataset_id   bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            published_at timestamptz NOT NULL DEFAULT now(),
            action       text NOT NULL CHECK (action IN ('publish', 'rollback'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE player_team_seasons (
            id                       bigserial PRIMARY KEY,
            dataset_id               bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            player_id                text NOT NULL,
            team_id                  text NOT NULL,
            season_id                integer NOT NULL,
            -- Provenance only (which CSV source's stats won the import-time
            -- merge) -- never part of the row's football identity. See the
            -- module docstring, "Why this grain, and not source_file or
            -- league_id".
            source_file              text NOT NULL CHECK (source_file IN ('player_season_stats', 'player_history')),
            player_name              text,
            name_status              text NOT NULL CHECK (name_status IN ('known', 'masked', 'missing')),
            team_name                text,
            -- Display text, possibly multi-valued (comma-joined) when this
            -- team-season spanned more than one league -- see
            -- team_season_leagues for the normalized, per-league form.
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
            stats_available          boolean NOT NULL,
            stats_source             text,
            stats_completeness       text NOT NULL CHECK (stats_completeness IN ('full', 'partial', 'unavailable')),
            -- The true football grain: one row per player/team/season, full
            -- stop. source_file is deliberately NOT part of this constraint.
            UNIQUE (dataset_id, player_id, team_id, season_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_pts_dataset_player ON player_team_seasons (dataset_id, player_id)")
    op.execute("CREATE INDEX ix_pts_dataset_team_season ON player_team_seasons (dataset_id, team_id, season_id)")
    op.execute("CREATE INDEX ix_pts_dataset_season ON player_team_seasons (dataset_id, season_id)")

    op.execute(
        """
        CREATE TABLE team_season_leagues (
            dataset_id   bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            team_id      text NOT NULL,
            season_id    integer NOT NULL,
            league_id    integer NOT NULL REFERENCES leagues(league_id),
            PRIMARY KEY (dataset_id, team_id, season_id, league_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_tsl_dataset_team_season ON team_season_leagues (dataset_id, team_id, season_id)")

    op.execute(
        """
        CREATE TABLE player_origin_estimates (
            dataset_id                bigint NOT NULL REFERENCES dataset_versions(dataset_id),
            player_id                 text NOT NULL,
            algorithm_version         text NOT NULL,
            likely_origin_city        text,
            confidence                text,
            score                     double precision,
            candidates                jsonb,
            basis                     jsonb,
            first_registered_season   text,
            computed_at               timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (dataset_id, player_id, algorithm_version)
        )
        """
    )


def downgrade():
    # Reverse dependency order: fact/derived tables before the dimensions and
    # dataset bookkeeping they reference.
    op.execute("DROP TABLE IF EXISTS player_origin_estimates")
    op.execute("DROP TABLE IF EXISTS team_season_leagues")
    op.execute("DROP TABLE IF EXISTS player_team_seasons")
    op.execute("DROP TABLE IF EXISTS dataset_publication_log")
    op.execute("DROP TABLE IF EXISTS current_dataset")
    op.execute("DROP TABLE IF EXISTS dataset_versions")
    op.execute("DROP TABLE IF EXISTS leagues")
    op.execute("DROP TABLE IF EXISTS teams")
    op.execute("DROP TABLE IF EXISTS venues")
    op.execute("DROP TABLE IF EXISTS players")
    op.execute("DROP TABLE IF EXISTS seasons")
