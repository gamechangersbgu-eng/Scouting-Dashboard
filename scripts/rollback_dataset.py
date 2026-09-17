"""Roll ``current_dataset`` back to the dataset that was live before the current one.

Reads the published history from ``dataset_publication_log`` rather than
assuming "previous" means "dataset_id - 1": a rollback itself is logged as an
action, so rolling back twice in a row moves to whichever dataset was live
two publish/rollback events ago, not back and forth between the same two
datasets forever.

Usage:
    python -m scripts.rollback_dataset
    python -m scripts.rollback_dataset --to-dataset-id 3
"""

import argparse
import logging

from ifa_scraper import db

log = logging.getLogger("scripts.rollback_dataset")


def _dataset_live_before_current(cursor, current_dataset_id):
    """The most recent dataset_id in the publication log that was not the current one."""
    cursor.execute(
        """
        SELECT dataset_id FROM dataset_publication_log
        WHERE dataset_id != %s
        ORDER BY published_at DESC, id DESC
        LIMIT 1
        """,
        (current_dataset_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def rollback(to_dataset_id=None, database_url=None):
    with db.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT dataset_id FROM current_dataset WHERE id")
            row = cursor.fetchone()
            current_dataset_id = row[0] if row else None
            if current_dataset_id is None:
                raise RuntimeError("no dataset is currently published; nothing to roll back")

            if to_dataset_id is None:
                to_dataset_id = _dataset_live_before_current(cursor, current_dataset_id)
                if to_dataset_id is None:
                    raise RuntimeError("no earlier published dataset found to roll back to")

            cursor.execute("SELECT status FROM dataset_versions WHERE dataset_id = %s", (to_dataset_id,))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"dataset_id={to_dataset_id} does not exist")

            cursor.execute("UPDATE dataset_versions SET status = 'live' WHERE dataset_id = %s", (to_dataset_id,))
            cursor.execute(
                "UPDATE dataset_versions SET status = 'validated' WHERE dataset_id = %s AND status = 'live'",
                (current_dataset_id,),
            )
            cursor.execute(
                """
                INSERT INTO current_dataset (dataset_id) VALUES (%s)
                ON CONFLICT (id) DO UPDATE SET dataset_id = EXCLUDED.dataset_id
                """,
                (to_dataset_id,),
            )
            cursor.execute(
                "INSERT INTO dataset_publication_log (dataset_id, action) VALUES (%s, 'rollback')",
                (to_dataset_id,),
            )
        connection.commit()

    log.info("rolled back from dataset_id=%s to dataset_id=%s", current_dataset_id, to_dataset_id)
    return current_dataset_id, to_dataset_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to-dataset-id", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    rollback(to_dataset_id=args.to_dataset_id)


if __name__ == "__main__":
    main()
