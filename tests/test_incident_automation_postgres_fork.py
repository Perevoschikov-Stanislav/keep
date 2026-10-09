"""PostgreSQL serialization, durable claims and additive automation migration."""

import importlib.util
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, select

from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_automation import IncidentAutomationOperation
from keep.api.models.db.workflow import WorkflowExecution
from tests import test_incident_automation_fork as automation_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    url = make_url(DSN)
    if url.host != "127.0.0.1" or url.database != "silences_check":
        raise RuntimeError("Automation tests require the private loopback PostgreSQL sidecar")


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentAutomationTest(automation_tests.IncidentAutomationTest):
    def setUp(self):
        schema = "automation_check_" + uuid4().hex
        timeouts = " -clock_timeout=20000 -cstatement_timeout=60000"
        bootstrap = create_engine(DSN, connect_args={"options": timeouts})
        self.addCleanup(bootstrap.dispose)
        with bootstrap.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        def cleanup():
            with bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        self.addCleanup(cleanup)
        engine = create_engine(DSN, connect_args={"options": "-csearch_path=" + schema + timeouts}, pool_size=10, max_overflow=0)
        self.enterContext(patch("tests.team_fork_test_case.create_engine", return_value=engine))
        super().setUp()

    def test_manual_metadata_during_provider_enrichment_is_preserved(self):
        super().test_manual_metadata_during_provider_enrichment_is_preserved()
        self.assertEqual(self.engine.pool.checkedout(), 0, "Provider enrichment retained a database transaction")

    def test_failed_provider_enrichment_releases_its_database_transaction(self):
        self.ticket()
        self.document["workflow"]["actions"][0]["provider"]["with"]["enrich_incident"] = [{"key": "ticket_url", "value": "https://generated.example.org/T-1"}]
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        self.correlate(self.event())
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", return_value={"ticket_id": "T/1"}), \
             patch("keep.api.bl.enrichments_bl.EnrichmentsBl._enrich_incident", side_effect=RuntimeError("Database enrichment failed")):
            self.tick(0, execute=True)
        self.assertEqual(self.operations()[0].status, "failed")
        self.assertEqual(self.engine.pool.checkedout(), 0, "Failed enrichment retained a database transaction")

    def concurrent(self, function, count=8):
        barrier = threading.Barrier(count)
        def run(index):
            barrier.wait(timeout=20)
            return function(index)
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(run, range(count)))

    def test_eight_workers_materialize_one_operation_and_one_level_transition(self):
        self.correlate(self.event())
        self.concurrent(lambda _: self.worker().run_once(now=self.now(10), execute=False))
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.incidents()[0].automation_context["cursors"], {"first": 0})

    def test_eight_claims_have_one_winner_and_one_workflow_execution(self):
        self.correlate(self.event())
        self.tick(10)
        identifier = self.operations()[0].id
        claims = self.concurrent(lambda _: self.worker().claim(identifier, self.now(10)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(WorkflowExecution)).all()), 1)

    def test_expired_unstarted_lease_recovery_fences_old_owner(self):
        self.correlate(self.event())
        self.tick(10)
        old = self.worker().claim(self.operations()[0].id, self.now(10))
        claims = self.concurrent(lambda _: self.worker().claim(old.id, self.now(311)))
        new = next(item for item in claims if item)
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertNotEqual(old.token, new.token)
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.worker().execute(old, self.now(311))
        send.assert_not_called()
        self.assertEqual(self.operations()[0].token, new.token)
        self.assertEqual(self.operations()[0].status, "running")

    def test_ack_between_claim_and_execution_blocks_the_provider(self):
        self.correlate(self.event())
        self.tick(10)
        claimed = self.worker().claim(self.operations()[0].id, self.now(10))
        self.change("acknowledged", 11)
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.worker().execute(claimed, self.now(11))
        send.assert_not_called()
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_different_operations_for_one_nonparallel_workflow_have_one_winner(self):
        self.correlate(self.event())
        self.tick(10)
        self.tick(15)
        identifiers = [row.id for row in self.operations()]
        claims = self.concurrent(lambda index: self.worker().claim(identifiers[index], self.now(15)), count=2)
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(sorted(row.status for row in self.operations()), ["pending", "running"])

    def test_actual_migration_preserves_manual_incident_and_refuses_loss(self):
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            row.user_summary = "Manual note"
            session.add(row)
            session.commit()
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE incidentautomationoperation"))
            connection.execute(text("ALTER TABLE incident DROP COLUMN automation_context"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-05-23-00_6b2fda31c890.py"
        spec = importlib.util.spec_from_file_location("automation_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            self.assertEqual(row.user_summary, "Manual note")
            self.assertIsNone(row.automation_context)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        self.tick(10)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "Automation state exists"):
                migration.downgrade()
