"""Validation rules a candidate dataset must pass before it can be published.

Run by ``scripts/import_canonical_dataset.py`` against the dataset it just
built (never against the live one), and re-runnable standalone by
``scripts/publish_dataset.py`` as a final check immediately before flipping
``current_dataset``. Every rule here is something the *database itself*
cannot already guarantee via a CHECK/UNIQUE constraint on its own -- pure
row-shape constraints (non-negative stats, valid source_file, valid
name_status/stats_completeness, the natural key's uniqueness) are already
enforced by ``migrations/versions/0001_canonical_schema.py`` and are not
duplicated here.

Deliberately NOT enforced (and why): "goals <= games" is invalid under the
IFA's own semantics -- a player can score more than once in a single game --
so no rule here checks it, and nothing in this module should ever be
extended to check it without re-confirming that against real data first.
"""

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def add_error(self, message):
        self.ok = False
        self.errors.append(message)

    def add_warning(self, message):
        self.warnings.append(message)


# A published dataset dropping below this fraction of the previous live
# dataset's player or player_team_seasons row count is treated as a probable
# scrape failure rather than a genuine roster change, and blocks publication.
MIN_RETAINED_FRACTION = 0.5


def validate_dataset(connection, dataset_id, previous_dataset_id=None):
    """Run every validation rule against ``dataset_id``; never against the live one.

    ``connection`` is an open psycopg connection with at least read access to
    the candidate dataset's rows (the publisher role). Returns a
    ``ValidationResult``; callers decide what to do with a failing result
    (``import_canonical_dataset.py`` marks the dataset ``failed`` and stops;
    it is never marked ``live``).
    """
    result = ValidationResult(ok=True)
    with connection.cursor() as cursor:
        _check_referential_integrity(cursor, dataset_id, result)
        _check_starts_and_substitutions_within_games(cursor, dataset_id, result)
        _check_no_masked_over_known_regression(cursor, dataset_id, result)
        _check_row_count_drop(cursor, dataset_id, previous_dataset_id, result)
        _check_season_coverage(cursor, dataset_id, previous_dataset_id, result)
        _check_observation_referential_integrity(cursor, dataset_id, result)
        _check_every_canonical_row_has_an_observation(cursor, dataset_id, result)
        _check_observation_row_count_drop(cursor, dataset_id, previous_dataset_id, result)
    return result


def _check_referential_integrity(cursor, dataset_id, result):
    """Every player_team_seasons row must reference a real player/team/season.

    There is deliberately no database-level FK from player_team_seasons to
    players/teams/seasons: the importer upserts the dimension tables and the
    fact table in the same transaction (see import_canonical_dataset.py), and
    a hard FK would make row insertion order matter more than it should. This
    check is what actually guarantees the relationship instead.
    """
    cursor.execute(
        """
        SELECT count(*) FROM player_team_seasons pts
        WHERE pts.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM players p WHERE p.player_id = pts.player_id)
        """,
        (dataset_id,),
    )
    (missing_players,) = cursor.fetchone()
    if missing_players:
        result.add_error(f"{missing_players} player_team_seasons rows reference an unknown player_id")

    cursor.execute(
        """
        SELECT count(*) FROM player_team_seasons pts
        WHERE pts.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM teams t WHERE t.team_id = pts.team_id)
        """,
        (dataset_id,),
    )
    (missing_teams,) = cursor.fetchone()
    if missing_teams:
        result.add_error(f"{missing_teams} player_team_seasons rows reference an unknown team_id")

    cursor.execute(
        """
        SELECT count(*) FROM player_team_seasons pts
        WHERE pts.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM seasons s WHERE s.season_id = pts.season_id)
        """,
        (dataset_id,),
    )
    (missing_seasons,) = cursor.fetchone()
    if missing_seasons:
        result.add_error(f"{missing_seasons} player_team_seasons rows reference an unknown season_id")


def _check_starts_and_substitutions_within_games(cursor, dataset_id, result):
    """starts, sub_on and sub_off can never individually exceed games played.

    Unlike goals (a player can score more than once per game), a player can
    start, come on, or go off at most once per game, so each of these three
    counts bounded by games is a genuine data-quality invariant.
    """
    cursor.execute(
        """
        SELECT player_id, team_id, season_id, source_file, games, starts, sub_on, sub_off
        FROM player_team_seasons
        WHERE dataset_id = %s
          AND stats_available
          AND (
              (starts IS NOT NULL AND games IS NOT NULL AND starts > games)
              OR (sub_on IS NOT NULL AND games IS NOT NULL AND sub_on > games)
              OR (sub_off IS NOT NULL AND games IS NOT NULL AND sub_off > games)
          )
        LIMIT 20
        """,
        (dataset_id,),
    )
    offenders = cursor.fetchall()
    if offenders:
        result.add_error(
            f"{len(offenders)}+ rows have starts/sub_on/sub_off greater than games "
            f"(e.g. player_id={offenders[0][0]!r} team_id={offenders[0][1]!r} "
            f"season_id={offenders[0][2]!r} source_file={offenders[0][3]!r})"
        )


def _check_no_masked_over_known_regression(cursor, dataset_id, result):
    """A player already known by name must never come out of an import as masked.

    This is the database-level backstop for the masked-name merge policy in
    ifa_scraper.name_quality: the importer itself must never regress a
    players.name_status from 'known' to 'masked'/'missing' on upsert (see
    import_canonical_dataset.py), but this check catches it if it ever does.
    """
    cursor.execute(
        """
        SELECT count(*) FROM player_team_seasons pts
        JOIN players p ON p.player_id = pts.player_id
        WHERE pts.dataset_id = %s
          AND pts.name_status = 'known'
          AND p.name_status != 'known'
        """,
        (dataset_id,),
    )
    (regressed,) = cursor.fetchone()
    if regressed:
        result.add_error(
            f"{regressed} players have a known name in this dataset's rows but a "
            f"non-known name_status on the players dimension row -- the importer's "
            f"upsert must never let a masked/missing name win over a known one"
        )


def _check_row_count_drop(cursor, dataset_id, previous_dataset_id, result):
    """A catastrophic drop in player or row count usually means a broken scrape."""
    if previous_dataset_id is None:
        result.add_warning("no previous live dataset to compare row counts against")
        return

    cursor.execute(
        "SELECT count(DISTINCT player_id), count(*) FROM player_team_seasons WHERE dataset_id = %s",
        (dataset_id,),
    )
    candidate_players, candidate_rows = cursor.fetchone()
    cursor.execute(
        "SELECT count(DISTINCT player_id), count(*) FROM player_team_seasons WHERE dataset_id = %s",
        (previous_dataset_id,),
    )
    previous_players, previous_rows = cursor.fetchone()

    if previous_players and candidate_players < previous_players * MIN_RETAINED_FRACTION:
        result.add_error(
            f"player count dropped from {previous_players} to {candidate_players} "
            f"(below {MIN_RETAINED_FRACTION:.0%} of the previous live dataset)"
        )
    if previous_rows and candidate_rows < previous_rows * MIN_RETAINED_FRACTION:
        result.add_error(
            f"player_team_seasons row count dropped from {previous_rows} to {candidate_rows} "
            f"(below {MIN_RETAINED_FRACTION:.0%} of the previous live dataset)"
        )


def _check_season_coverage(cursor, dataset_id, previous_dataset_id, result):
    """Every season the previous live dataset covered should still be present.

    A season disappearing entirely is a stronger signal of a broken scrape
    run than a lower row count within a season that is still there, so it is
    reported even when the row-count check above did not trigger.
    """
    if previous_dataset_id is None:
        return
    cursor.execute(
        """
        SELECT array_agg(DISTINCT season_id) FROM player_team_seasons WHERE dataset_id = %s
        """,
        (previous_dataset_id,),
    )
    (previous_seasons,) = cursor.fetchone()
    cursor.execute(
        """
        SELECT array_agg(DISTINCT season_id) FROM player_team_seasons WHERE dataset_id = %s
        """,
        (dataset_id,),
    )
    (candidate_seasons,) = cursor.fetchone()
    missing = sorted(set(previous_seasons or []) - set(candidate_seasons or []))
    if missing:
        result.add_error(f"season(s) present in the previous live dataset are missing here: {missing}")


def _check_observation_referential_integrity(cursor, dataset_id, result):
    """Every observation row must reference a real player/team/season, same as _check_referential_integrity.

    ``player_team_season_observations`` (see the 0002 migration) has no
    database-level FK either, for the same reason ``player_team_seasons``
    doesn't: the importer upserts dimensions and facts in one transaction and
    a hard FK would make insertion order matter more than it should.
    """
    cursor.execute(
        """
        SELECT count(*) FROM player_team_season_observations o
        WHERE o.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM players p WHERE p.player_id = o.player_id)
        """,
        (dataset_id,),
    )
    (missing_players,) = cursor.fetchone()
    if missing_players:
        result.add_error(
            f"{missing_players} player_team_season_observations rows reference an unknown player_id"
        )

    cursor.execute(
        """
        SELECT count(*) FROM player_team_season_observations o
        WHERE o.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM teams t WHERE t.team_id = o.team_id)
        """,
        (dataset_id,),
    )
    (missing_teams,) = cursor.fetchone()
    if missing_teams:
        result.add_error(
            f"{missing_teams} player_team_season_observations rows reference an unknown team_id"
        )

    cursor.execute(
        """
        SELECT count(*) FROM player_team_season_observations o
        WHERE o.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM seasons s WHERE s.season_id = o.season_id)
        """,
        (dataset_id,),
    )
    (missing_seasons,) = cursor.fetchone()
    if missing_seasons:
        result.add_error(
            f"{missing_seasons} player_team_season_observations rows reference an unknown season_id"
        )


def _check_every_canonical_row_has_an_observation(cursor, dataset_id, result):
    """Every player_team_seasons row must be backed by at least one observation.

    This is the specific invariant the 0002 migration exists to protect: a
    canonical row is always produced by merging one or two observation rows
    (see ``scripts.import_canonical_dataset.import_dataset``), so a canonical
    row with zero matching observations means the two writes -- observations
    and the canonical merge -- have drifted apart, which is exactly the class
    of bug that made source-membership unrecoverable before this migration.
    """
    cursor.execute(
        """
        SELECT count(*) FROM player_team_seasons pts
        WHERE pts.dataset_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM player_team_season_observations o
              WHERE o.dataset_id = pts.dataset_id
                AND o.player_id = pts.player_id
                AND o.team_id = pts.team_id
                AND o.season_id = pts.season_id
          )
        """,
        (dataset_id,),
    )
    (orphaned,) = cursor.fetchone()
    if orphaned:
        result.add_error(
            f"{orphaned} player_team_seasons rows have no matching "
            f"player_team_season_observations row at all -- the canonical merge "
            f"and the observation write have drifted apart"
        )


def _check_observation_row_count_drop(cursor, dataset_id, previous_dataset_id, result):
    """A catastrophic drop in either source's observation count usually means a broken import.

    Mirrors ``_check_row_count_drop`` but per source_file, since the two
    sources' row counts are independent (see the 0002 migration): a bug that
    only breaks writing one source's observations -- history and recent stats
    are written from separate CSVs -- would not necessarily move the combined
    ``player_team_seasons`` count enough to trip that check alone.
    """
    if previous_dataset_id is None:
        return

    cursor.execute(
        """
        SELECT source_file, count(*) FROM player_team_season_observations
        WHERE dataset_id = %s GROUP BY source_file
        """,
        (dataset_id,),
    )
    candidate_counts = dict(cursor.fetchall())
    cursor.execute(
        """
        SELECT source_file, count(*) FROM player_team_season_observations
        WHERE dataset_id = %s GROUP BY source_file
        """,
        (previous_dataset_id,),
    )
    previous_counts = dict(cursor.fetchall())

    for source_file, previous_count in previous_counts.items():
        candidate_count = candidate_counts.get(source_file, 0)
        if previous_count and candidate_count < previous_count * MIN_RETAINED_FRACTION:
            result.add_error(
                f"player_team_season_observations rows for source_file={source_file!r} "
                f"dropped from {previous_count} to {candidate_count} "
                f"(below {MIN_RETAINED_FRACTION:.0%} of the previous live dataset)"
            )
