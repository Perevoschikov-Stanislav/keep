"""Private k3d PostgreSQL import concurrency, crash recovery and additive migration."""

import importlib.util
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlmodel import Session

from tests.test_incident_notifications_postgres_fork import DSN, PostgresFixture
from tests import test_legacy_incident_migration_fork as migration_tests
from keep.api.models.db.incident_migration import LegacyIncidentImport


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresLegacyIncidentMigrationTest(PostgresFixture, migration_tests.LegacyIncidentMigrationTest):
    def test_concurrent_imports_return_one_import_and_seven_replays(self):
        self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        preview = self.preview(exported, plan)
        candidate = self.candidate()
        def apply(_):
            with Session(self.engine) as session:
                return self.migration.apply(session, exported, candidate, plan,
                    expected_preview_digest=preview["preview_digest"], now=self.now(2))
        results = self.concurrent(apply)
        self.assertEqual(sum(item["result"] == "imported" for item in results), 1)
        self.assertEqual(sum(item["result"] == "noop" for item in results), 7)
        self.assertEqual(len(self.rows(LegacyIncidentImport)), 1)

    def test_additive_migration_preserves_manual_state_and_blocks_receipt_downgrade(self):
        self.legacy()
        before = self.incidents()[0].dict()
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE legacyincidentimport"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-06-11-00_d39b72e4a610.py"
        spec = importlib.util.spec_from_file_location("legacy_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        self.assertEqual(before, self.incidents()[0].dict())
        exported = self.export()
        self.migrate(exported, self.manifest(exported))
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "receipts exist"):
                migration.downgrade()


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentRuntimeOwnershipTest(PostgresFixture, migration_tests.IncidentRuntimeOwnershipTest):
    pass
