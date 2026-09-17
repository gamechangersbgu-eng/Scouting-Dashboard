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


def run_migrations_offline():
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
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
