"""Additive migrations preserve existing alerts and refuse to discard audit."""

import importlib.util
import io
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION = Path(__file__).parents[1] / "keep/api/models/db/migrations/versions/2026-10-04-00-00_8c5f7e2a4d16.py"


def migration(path=MIGRATION):
    spec = importlib.util.spec_from_file_location("silences_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SilenceMigrationTest(unittest.TestCase):
    def test_empty_and_existing_database_upgrade_and_empty_downgrade(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                engine = create_engine("sqlite://")
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE tenant (id VARCHAR(256) PRIMARY KEY)"))
                    connection.execute(text("CREATE TABLE alert (id TEXT PRIMARY KEY, event TEXT)"))
                    if existing:
                        connection.execute(text("INSERT INTO tenant VALUES ('keep')"))
                        connection.execute(text("INSERT INTO alert VALUES ('old', 'preserved')"))
                    context = MigrationContext.configure(connection)
                    with Operations.context(context):
                        migration().upgrade()
                    names = set(inspect(connection).get_table_names())
                    self.assertTrue({"silence", "silencecommand", "silenceevent"} <= names)
                    self.assertEqual({index["name"] for index in inspect(connection).get_indexes("silence")},
                        {"ix_silence_tenant_team_period", "ix_silence_tenant_created"})
                    with Operations.context(context):
                        migration().downgrade()
                    self.assertNotIn("silence", inspect(connection).get_table_names())
                    if existing:
                        self.assertEqual(connection.execute(text("SELECT event FROM alert")).scalar(), "preserved")
                engine.dispose()

    def test_nonempty_downgrade_is_refused_before_any_drop(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tenant (id VARCHAR(256) PRIMARY KEY)"))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration().upgrade()
            # Seed an audit row using SQL's JSON representation to exercise the recovery guard.
            connection.execute(text("INSERT INTO silenceevent VALUES (:id, 'keep', 'alpha', :id, 1, 'silence.created', '2026-10-04 12:00:00', '{}')"), {"id": "a" * 32})
            with Operations.context(context), self.assertRaisesRegex(RuntimeError, "Silence data exists"):
                migration().downgrade()
            self.assertTrue({"silence", "silencecommand", "silenceevent"} <= set(inspect(connection).get_table_names()))
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM silenceevent")).scalar(), 1)
        engine.dispose()

    def test_mysql_postgresql_ddl_and_offline_downgrade_guard(self):
        for dialect in ("mysql", "postgresql"):
            output = io.StringIO()
            context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": output})
            with Operations.context(context):
                migration().upgrade()
            ddl = output.getvalue()
            self.assertIn("CREATE TABLE silence", ddl)
            self.assertIn("uq_silence_event_revision", ddl)
            self.assertIn("PRIMARY KEY (tenant_id, actor_scope, client_request_id)", ddl)
            if dialect == "mysql":
                self.assertIn("DATETIME(6)", ddl)
            with Operations.context(context), self.assertRaisesRegex(RuntimeError, "Offline downgrade"):
                migration().downgrade()
