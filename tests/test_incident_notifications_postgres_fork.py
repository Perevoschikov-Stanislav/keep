"""Real PostgreSQL fencing, serialized bindings, command races and migration."""

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
from keep.api.models.db.incident_notification import IncidentIntegrationCommand
from keep.api.models.db.silence import NotificationDelivery
from tests import test_incident_notifications_fork as notification_tests
from tests import test_incident_notifications_api_fork as api_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    url = make_url(DSN)
    if url.host != "127.0.0.1" or url.database != "silences_check":
        raise RuntimeError("Notification tests require the private loopback PostgreSQL sidecar")


class PostgresFixture:
    def setUp(self):
        schema = "notification_check_" + uuid4().hex
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

    def concurrent(self, function, count=8):
        barrier = threading.Barrier(count)
        def run(index):
            barrier.wait(timeout=20)
            return function(index)
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(run, range(count)))


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentNotificationsTest(PostgresFixture, notification_tests.IncidentNotificationsTest):
    def test_concurrent_scans_after_expiry_publish_one_fresh_projection_per_destination(self):
        self.resume_after_silence()
        self.correlate(self.event())
        self.silence_rule()
        self.dispatcher(1)._scan()
        self.concurrent(lambda _: self.dispatcher(21)._scan())
        rows = self.deliveries()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row.state == "pending" for row in rows), 2)
        self.assertEqual(self.incidents()[0].notification_context["sequence"], 2)

    def test_eight_scanners_publish_only_one_projection_per_destination(self):
        self.correlate(self.event())
        self.concurrent(lambda _: self.dispatcher()._scan())
        self.assertEqual(len(self.deliveries()), 2)
        self.assertEqual(self.incidents()[0].notification_context["sequence"], 1)

    def test_parallel_claims_lease_each_delivery_once(self):
        self.correlate(self.event())
        self.dispatcher()._scan()
        claims = self.concurrent(lambda _: self.dispatcher().claim())
        identifiers = [row.id for row in claims if row]
        self.assertEqual(len(identifiers), 2)
        self.assertEqual(len(set(identifiers)), 2)
        self.assertTrue(all(row.attempts == 1 for row in self.deliveries()))

    def test_concurrent_duplicate_commands_have_one_mutation_and_one_receipt(self):
        from keep.api.core.incident_notifications import execute_incident_command
        self.correlate(self.event())
        command = self.command()
        def execute(_):
            with Session(self.engine) as session:
                return execute_incident_command(session, self.actor(), command, now=self.now(1))
        results = self.concurrent(execute)
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(self.incidents()[0].lifecycle_context["revision"], 1)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(IncidentIntegrationCommand)).all()), 1)

    def test_postgres_additive_migration_preserves_legacy_delivery_and_manual_incident(self):
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        delivery_id = uuid4()
        with Session(self.engine) as session:
            incident = session.get(Incident, identifier)
            incident.user_summary = "Manual incident note"
            session.add(incident)
            session.add(NotificationDelivery(id=delivery_id, tenant_id="tenant", team_id="alpha", event_id=uuid4(),
                subscriber_id="legacy-silence", destination_id="alpha-http", transport_id="webhook", policy_digest="old",
                payload={"schema_version": 1, "event_type": "silence.created"}, available_at=self.now(0), created_at=self.now(0)))
            session.commit()
        with self.engine.begin() as connection:
            for table in ("incidentintegrationcommand", "incidentnotificationcursor", "incidentnotificationbinding"):
                connection.execute(text("DROP TABLE " + table))
            connection.execute(text("ALTER TABLE incident DROP COLUMN notification_context"))
            connection.execute(text("ALTER TABLE notificationdelivery DROP COLUMN context"))
            connection.execute(text("ALTER TABLE notificationdelivery DROP COLUMN effect_started"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-06-08-00_a46c093ef271.py"
        spec = importlib.util.spec_from_file_location("notification_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            self.assertEqual(session.get(Incident, identifier).user_summary, "Manual incident note")
            self.assertIsNone(session.get(Incident, identifier).notification_context)
            row = session.get(NotificationDelivery, delivery_id)
            self.assertEqual(row.context, {})
            self.assertFalse(row.effect_started)
            self.assertEqual(row.payload["event_type"], "silence.created")
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        self.dispatcher()._scan()
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "Notification state exists"):
                migration.downgrade()


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentNotificationsApiTest(PostgresFixture, api_tests.IncidentNotificationsApiTest):
    pass
