"""Atomically publish a validated dataset as the live one.

Publication never renames or rebuilds a schema: it re-validates the
candidate one last time (the world may have changed since import), then
flips ``current_dataset.dataset_id`` to it inside one small transaction,
alongside marking the dataset ``live`` and appending a
``dataset_publication_log`` row. A dataset is never exposed to the app
partially built -- ``current_dataset`` only ever points at a dataset whose
import has fully committed and whose validation passed.

Usage:
    python -m scripts.publish_dataset --dataset-id 4
    python -m scripts.publish_dataset --latest-validated
"""

import argparse
import logging

from ifa_scraper import db, validation

log = logging.getLogger("scripts.publish_dataset")


def _latest_validated_dataset_id(cursor):
    cursor.execute(
        "SELECT dataset_id FROM dataset_versions WHERE status = 'validated' ORDER BY dataset_id DESC LIMIT 1"
    )
    row = cursor.fetchone()
    return row[0] if row else None


def publish(dataset_id=None, database_url=None):
    with db.connect(database_url) as connection:
        with connection.cursor() as cursor:
            if dataset_id is None:
                dataset_id = _latest_validated_dataset_id(cursor)
                if dataset_id is None:
                    raise RuntimeError("no dataset with status='validated' to publish")

            cursor.execute("SELECT status FROM dataset_versions WHERE dataset_id = %s", (dataset_id,))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"dataset_id={dataset_id} does not exist")
            (status,) = row
            if status not in ("validated", "live"):
                raise RuntimeError(
                    f"dataset_id={dataset_id} has status={status!r}; only a 'validated' "
                    f"dataset can be published (run scripts/import_canonical_dataset.py first)"
                )

            cursor.execute("SELECT dataset_id FROM current_dataset WHERE id")
            row = cursor.fetchone()
            previous_dataset_id = row[0] if row else None

        # Re-validate immediately before publishing: nothing should have changed
        # since import, but this is the last chance to catch it before the app
        # sees this dataset.
        result = validation.validate_dataset(connection, dataset_id, previous_dataset_id)
        if not result.ok:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE dataset_versions SET status = 'failed', notes = %s WHERE dataset_id = %s",
                    ("; ".join(result.errors), dataset_id),
                )
            connection.commit()
            raise RuntimeError(
                f"dataset_id={dataset_id} failed re-validation at publish time and was NOT published: "
                + "; ".join(result.errors)
            )

        with connection.cursor() as cursor:
            cursor.execute("UPDATE dataset_versions SET status = 'live' WHERE dataset_id = %s", (dataset_id,))
            if previous_dataset_id is not None and previous_dataset_id != dataset_id:
                cursor.execute(
                    "UPDATE dataset_versions SET status = 'validated' WHERE dataset_id = %s AND status = 'live'",
                    (previous_dataset_id,),
                )
            cursor.execute(
                """
                INSERT INTO current_dataset (dataset_id) VALUES (%s)
                ON CONFLICT (id) DO UPDATE SET dataset_id = EXCLUDED.dataset_id
                """,
                (dataset_id,),
            )
            cursor.execute(
                "INSERT INTO dataset_publication_log (dataset_id, action) VALUES (%s, 'publish')",
                (dataset_id,),
            )
        connection.commit()

    log.info("published dataset_id=%s (previous live dataset_id=%s)", dataset_id, previous_dataset_id)
    return dataset_id, previous_dataset_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dataset-id", type=int)
    group.add_argument("--latest-validated", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    publish(dataset_id=args.dataset_id)


if __name__ == "__main__":
    main()
