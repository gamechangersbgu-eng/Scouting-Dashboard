"""Shared transaction-scoped validation for built canonical datasets."""

from ifa_scraper import validation


LOCAL_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '30min'"


def set_local_statement_timeout(cursor):
    """Allow one build or validation transaction up to 30 minutes.

    ``SET LOCAL`` resets at COMMIT/ROLLBACK. It never changes the database-wide
    setting, so each separate connection must issue it independently.
    """
    cursor.execute(LOCAL_STATEMENT_TIMEOUT_SQL)


def validate_and_record_dataset(connection, dataset_id):
    """Validate one existing dataset and persist its outcome.

    The caller owns the transaction and must commit it. This routine neither
    builds data nor publishes it.
    """
    with connection.cursor() as cursor:
        set_local_statement_timeout(cursor)
        cursor.execute(
            "SELECT status FROM dataset_versions WHERE dataset_id = %s",
            (dataset_id,),
        )
        if cursor.fetchone() is None:
            raise RuntimeError(f"dataset_id={dataset_id} does not exist")

        cursor.execute("SELECT dataset_id FROM current_dataset WHERE id")
        row = cursor.fetchone()
    previous_dataset_id = row[0] if row else None

    result = validation.validate_dataset(connection, dataset_id, previous_dataset_id)
    status = "validated" if result.ok else "failed"
    notes = "; ".join(result.errors) or None
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE dataset_versions SET status = %s, finished_at = now(), notes = %s WHERE dataset_id = %s",
            (status, notes, dataset_id),
        )
    return result
