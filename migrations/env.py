"""Alembic environment for the canonical scouting-data schema.

Deliberately minimal: no ORM models, no autogenerate. Every migration in
versions/ is hand-written SQL (op.execute(...)) because the canonical schema
was designed and verified directly against PostgreSQL (see the implementation
report), and autogenerate has nothing to compare against without a
declarative model layer this project does not have.

The connection string is read only from the DATABASE_URL environment
variable -- never from alembic.ini -- so it never needs to be committed.
Migrations are run by an operator (or a deploy step) with the *publisher*
database role, never by the web application itself.

DATABASE_URL itself always stays a plain ``postgresql://`` (or legacy
``postgres://``) URL -- that is the exact string ``ifa_scraper/db.py`` hands
straight to ``psycopg.connect()``, and it must keep working there unchanged.
SQLAlchemy, however, defaults an unqualified ``postgresql://`` scheme to the
psycopg2 dialect, and this project intentionally installs psycopg v3
(``psycopg[binary]==3.2.10``, see requirements.txt) instead, with no
psycopg2 installed at all. Left alone, that mismatch makes
``engine_from_config``/``context.configure`` below try to import psycopg2
and fail with ``ModuleNotFoundError: No module named 'psycopg2'``.
``_sqlalchemy_url()`` is the fix: it rewrites only the URL SQLAlchemy sees
(to ``postgresql+psycopg://``, the explicit psycopg v3 dialect), and only in
memory here -- DATABASE_URL in the environment is never touched, and this
value is never routed through ``config.set_main_option()`` (which would
apply ConfigParser ``%`` interpolation to it), so there is nothing here for a
literal ``%`` in a password to break.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project importable (config.py etc.) if this is ever run from
# outside the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # No ORM models: every migration is explicit SQL.


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL must be set to run migrations (never stored in alembic.ini)"
        )
    return url


def _sqlalchemy_url(raw_url):
    """Rewrite a plain postgresql(+legacy postgres):// URL for psycopg v3.

    Pure string transform, no I/O -- kept separate from ``_database_url()``
    so the two responsibilities (reading DATABASE_URL, adapting it for
    SQLAlchemy) stay independently testable. Only the scheme changes; a URL
    that already names a dialect (``postgresql+psycopg://``, or any other
    ``postgresql+...://``) is returned unchanged rather than rewritten again.
    """
    if raw_url.startswith("postgresql+"):
        return raw_url
    if raw_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw_url[len("postgresql://"):]
    if raw_url.startswith("postgres://"):
        return "postgresql+psycopg://" + raw_url[len("postgres://"):]
    return raw_url


def run_migrations_offline():
    context.configure(
        url=_sqlalchemy_url(_database_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sqlalchemy_url(_database_url())
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
