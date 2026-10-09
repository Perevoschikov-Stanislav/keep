"""Task 25 on the private k3d PostgreSQL database, including the real migration."""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

from keep.api.models.db.incident import Incident
from keep.api.models.db.tenant import Tenant
from tests import test_event_normalization_fork as normalization_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    url = make_url(DSN)
    if url.host != "127.0.0.1" or url.database != "silences_check":
        raise RuntimeError("Normalization checks require the private loopback PostgreSQL sidecar")


@unittest.skipUnless(DSN, "Requires the isolated k3d PostgreSQL sidecar")
class PostgresEventNormalizationTest(normalization_tests.EventNormalizationTest):
    def setUp(self):
        schema = "normalization_check_" + uuid4().hex
        bootstrap = create_engine(DSN)
        self.addCleanup(bootstrap.dispose)
        with bootstrap.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        def cleanup():
            with bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        self.addCleanup(cleanup)
        engine = create_engine(DSN, connect_args={"options": "-csearch_path=" + schema}, pool_size=5)
        self.enterContext(patch("tests.team_fork_test_case.create_engine", return_value=engine))
        super().setUp()

    def apply(self, *args, **kwargs):
        with Session(self.engine) as session:
            if session.get(Tenant, "tenant") is None:
                session.add(Tenant(id="tenant", name="Normalization fixture"))
                session.commit()
        return super().apply(*args, **kwargs)

    def test_actual_migration_preserves_legacy_manual_data_and_guards_downgrade(self):
        with Session(self.engine) as session:
            legacy = Incident(tenant_id="tenant", team_id="alpha", user_generated_name="Manual title", user_summary="Notes", assignee="engineer")
            session.add(legacy)
            session.commit()
            identifier = legacy.id
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE incident DROP COLUMN generated_name"))
            connection.execute(text("ALTER TABLE incident DROP COLUMN normalization_context"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-05-18-00_c27f8d21b409.py"
        spec = importlib.util.spec_from_file_location("normalization_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            self.assertEqual((row.user_generated_name, row.user_summary, row.assignee), ("Manual title", "Notes", "engineer"))
            self.assertIsNone(row.generated_name)
            self.assertIsNone(row.normalization_context)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
            connection.execute(text("UPDATE incident SET generated_name = 'Generated'"))
            with self.assertRaisesRegex(RuntimeError, "Derived presentation exists"):
                migration.downgrade()
        self.assertIn("generated_name", {column["name"] for column in inspect(self.engine).get_columns("incident")})
