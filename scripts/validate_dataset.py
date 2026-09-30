"""Validate an already-built dataset without importing or publishing it.

Usage:
    python -m scripts.validate_dataset --dataset-id 6
    python -m scripts.validate_dataset --dataset-id 6 --analyze
"""

import argparse
import logging

from ifa_scraper import db
from scripts.dataset_validation import (
    set_local_statement_timeout,
    validate_and_record_dataset,
)

log = logging.getLogger("scripts.validate_dataset")


def _analyze_validation_tables(connection):
    """Refresh planner statistics in a separate maintenance transaction."""
    with connection.cursor() as cursor:
        set_local_statement_timeout(cursor)
        cursor.execute("ANALYZE player_team_season_observations")
        cursor.execute("ANALYZE player_team_seasons")


def validate_existing_dataset(dataset_id, database_url=None, analyze=False):
    """Validate and record an existing dataset; never import or publish it."""
    if analyze:
        # Commit before validation so the new planner statistics are visible to
        # the fresh validation connection.
        with db.connect(database_url) as maintenance_connection:
            _analyze_validation_tables(maintenance_connection)
            maintenance_connection.commit()

    with db.connect(database_url) as connection:
        result = validate_and_record_dataset(connection, dataset_id)
        connection.commit()

    for warning in result.warnings:
        log.warning("validation warning: %s", warning)
    if result.ok:
        log.info("dataset_id=%s validated successfully; it was not published", dataset_id)
    else:
        log.error("dataset_id=%s FAILED validation and was NOT published:", dataset_id)
        for error in result.errors:
            log.error("  - %s", error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", type=int, required=True)
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="ANALYZE the validation fact tables in a separate committed transaction first",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    result = validate_existing_dataset(args.dataset_id, analyze=args.analyze)
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
