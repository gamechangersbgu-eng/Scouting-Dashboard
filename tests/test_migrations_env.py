"""migrations/env.py: the DATABASE_URL -> SQLAlchemy-dialect URL rewrite.

migrations/env.py is Alembic's own environment script: by Alembic's own
convention it calls into ``alembic.context`` (and, in online mode,
``sqlalchemy``) unconditionally at the bottom of the file, the moment it is
loaded -- there is no function to call instead, that unconditional dispatch
*is* how Alembic runs a migration. That means it can never be imported for a
plain unit test in an environment without alembic/sqlalchemy installed
(this one, and this project's sandbox in general -- see the implementation
report on why psycopg/SQLAlchemy/Alembic cannot be pip-installed here).

So this test installs minimal stand-in modules for ``alembic`` and
``sqlalchemy`` in sys.modules before importing migrations/env.py, good
enough for that unconditional dispatch to run to completion as a no-op, and
then asserts against the one thing this test actually cares about: the pure,
side-effect-free ``_sqlalchemy_url()`` string transform defined in that real
file. Nothing here stands in for a real Alembic run -- that still has to
happen against a real database with the real packages installed, which is
explicitly not done by this test.
"""

import importlib
import os
import sys
import types
import unittest
from unittest import mock


def _install_alembic_and_sqlalchemy_stubs():
    context_module = types.ModuleType("alembic.context")
    context_module.config = mock.Mock()
    context_module.config.config_file_name = None
    context_module.is_offline_mode = mock.Mock(return_value=True)
    context_module.configure = mock.Mock()
    context_module.begin_transaction = mock.MagicMock()
    context_module.run_migrations = mock.Mock()

    alembic_module = types.ModuleType("alembic")
    alembic_module.context = context_module

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.engine_from_config = mock.Mock()
    sqlalchemy_module.pool = mock.Mock()

    sys.modules["alembic"] = alembic_module
    sys.modules["alembic.context"] = context_module
    sys.modules["sqlalchemy"] = sqlalchemy_module
    return context_module


class DatabaseUrlNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._context_stub = _install_alembic_and_sqlalchemy_stubs()
        # run_migrations_offline() -> _database_url() needs this set, or it
        # raises before the module finishes importing.
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/test"}):
            sys.modules.pop("migrations.env", None)
            cls.env = importlib.import_module("migrations.env")

    @classmethod
    def tearDownClass(cls):
        # Never let these stand-ins leak into other test modules that might
        # (now or later) import the real packages.
        for name in ("alembic", "alembic.context", "sqlalchemy"):
            sys.modules.pop(name, None)
        sys.modules.pop("migrations.env", None)

    def test_plain_postgresql_scheme_gets_psycopg_dialect(self):
        self.assertEqual(
            self.env._sqlalchemy_url("postgresql://user:pw@host/db"),
            "postgresql+psycopg://user:pw@host/db",
        )

    def test_legacy_postgres_scheme_gets_psycopg_dialect(self):
        self.assertEqual(
            self.env._sqlalchemy_url("postgres://user:pw@host/db"),
            "postgresql+psycopg://user:pw@host/db",
        )

    def test_explicit_psycopg_dialect_is_left_unchanged(self):
        url = "postgresql+psycopg://user:pw@host/db"
        self.assertEqual(self.env._sqlalchemy_url(url), url)

    def test_another_explicit_dialect_is_left_unchanged(self):
        # Not this project's dialect, but proves the rewrite only ever
        # touches a bare postgresql:// / postgres:// scheme, never a URL
        # that already names one.
        url = "postgresql+asyncpg://user:pw@host/db"
        self.assertEqual(self.env._sqlalchemy_url(url), url)

    def test_offline_migration_path_receives_the_rewritten_url(self):
        # The stubbed context.is_offline_mode() returned True, so importing
        # the module above already ran run_migrations_offline() once; this
        # confirms context.configure() was actually called with the
        # rewritten URL, not the raw DATABASE_URL.
        self._context_stub.configure.assert_called_once()
        self.assertEqual(
            self._context_stub.configure.call_args.kwargs["url"],
            "postgresql+psycopg://test/test",
        )


if __name__ == "__main__":
    unittest.main()
