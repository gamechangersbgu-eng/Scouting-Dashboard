"""Shared PostgreSQL connection helper for the canonical-data pipeline.

Mirrors the lazy-import, fail-soft convention already used by
``dashboard.auth.PostgresUserStore``: importing this module never requires
psycopg to be installed, only calling ``connect()`` does. That keeps the
default CSV-backed app -- and anything that merely imports this module in
passing -- working with no database driver present at all.

Used by:

* ``dashboard.postgres_source.PostgresScoutingData`` (read-only, ``app_reader``
  role in production)
* ``scripts/import_canonical_dataset.py``, ``scripts/publish_dataset.py`` and
  ``scripts/rollback_dataset.py`` (read/write, ``publisher`` role)
"""

import os


class DatabaseUnavailable(RuntimeError):
    """Raised when a PostgreSQL connection cannot be established or used."""


def resolve_database_url(database_url=None):
    return database_url or os.environ.get("DATABASE_URL")


def connect(database_url=None, connect_timeout=5):
    """Return a new psycopg connection, or raise ``DatabaseUnavailable``.

    Every caller is expected to use this as a context manager (``with
    db.connect(...) as connection:``), exactly like ``psycopg.connect`` itself,
    so a failure partway through a caller's work still closes the connection.
    """
    url = resolve_database_url(database_url)
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not configured")
    try:
        import psycopg
    except ImportError as exc:  # Keeps the CSV-only path working before install.
        raise DatabaseUnavailable("psycopg is not installed") from exc
    try:
        return psycopg.connect(url, connect_timeout=connect_timeout)
    except Exception as exc:
        raise DatabaseUnavailable("could not connect to the database") from exc


def current_dataset_id(connection):
    """Return the currently-published dataset_id, or ``None`` if nothing is live.

    A fresh database has no row in ``current_dataset`` at all until the first
    successful ``scripts/publish_dataset.py`` run, so "no live dataset" is an
    expected state to handle, not an error condition in itself.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT dataset_id FROM current_dataset WHERE id")
        row = cursor.fetchone()
        return row[0] if row else None
