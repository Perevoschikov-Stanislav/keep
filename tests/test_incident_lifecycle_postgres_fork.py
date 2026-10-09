"""PostgreSQL locking and additive lifecycle migration, in a private k3d DB."""

import importlib.util
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

from keep.api.core.incident_configuration import configuration_scope
from keep.api.core import incident_lifecycle as life
from keep.api.models.db.incident import Incident
from keep.rulesengine.rulesengine import RulesEngine
from tests import test_incident_lifecycle_fork as lifecycle_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    url = make_url(DSN)
    if url.host != "127.0.0.1" or url.database != "silences_check":
        raise RuntimeError("Lifecycle tests require the private loopback PostgreSQL sidecar")


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentLifecycleTest(lifecycle_tests.IncidentLifecycleTest):
    def setUp(self):
        schema = "lifecycle_check_" + uuid4().hex
        bootstrap = create_engine(DSN)
        self.addCleanup(bootstrap.dispose)
        with bootstrap.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        def cleanup():
            with bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        self.addCleanup(cleanup)
        engine = create_engine(DSN, connect_args={"options": "-csearch_path=" + schema}, pool_size=10, max_overflow=0)
        self.enterContext(patch("tests.team_fork_test_case.create_engine", return_value=engine))
        super().setUp()

    def concurrent(self, events):
        barrier = threading.Barrier(len(events))
        def run(event):
            with Session(self.engine) as session:
                barrier.wait(timeout=15)
                return RulesEngine("tenant").run_rules([event], session)
        with ThreadPoolExecutor(max_workers=len(events)) as pool:
            return list(pool.map(run, events))

    def test_concurrent_resolved_delivery_counts_once(self):
        self.configure()
        self.correlate(self.event())
        event = self.save(self.event(status="resolved"), 1)
        self.concurrent([event.copy(deep=True) for _ in range(8)])
        row = self.incidents()[0]
        self.assertEqual(row.status, "resolved")
        self.assertEqual(row.lifecycle_context["flapping"]["transition_count"], 1)
        self.assertEqual(len(self.audits()), 1)

    def test_concurrent_reopening_reuses_one_id_and_one_transition(self):
        self.configure()
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 1)
        events = [self.save(self.event("new-" + str(index)), 2) for index in range(8)]
        self.concurrent(events)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0].lifecycle_context["episode"], 2)
        self.assertEqual(self.incidents()[0].lifecycle_context["flapping"]["transition_count"], 2)
        self.assertEqual(len(self.audits()), 2)

    def test_concurrent_new_episode_resets_ack_once_and_keeps_group_flaps(self):
        self.configure(mode="new_incident", within=0)
        self.correlate(self.event())
        self.change("acknowledged", 1)
        old = self.incidents()[0].id
        self.correlate(self.event(status="resolved"), 2)
        events = [self.save(self.event("new-" + str(index)), 3) for index in range(8)]
        self.concurrent(events)
        self.assertEqual(len(self.incidents()), 2)
        new = next(row for row in self.incidents() if row.id != old)
        self.assertEqual(new.status, "firing")
        self.assertEqual(new.same_incident_in_the_past_id, old)
        self.assertTrue(new.lifecycle_context["flapping"]["active"])

    def test_concurrent_manual_cas_has_one_winner_and_one_audit(self):
        self.configure()
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        barrier = threading.Barrier(2)
        def run(status):
            with configuration_scope("tenant"), Session(self.engine) as session:
                barrier.wait(timeout=15)
                incident, group = life.lock_incident(session, "tenant", identifier, self.actor())
                try:
                    life.transition(session, incident, status, at=self.origin + timedelta(seconds=1),
                                    actor=self.actor().email, group=group, expected_revision=0)
                    session.commit()
                    return 200
                except HTTPException as error:
                    session.rollback()
                    return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["acknowledged", "resolved"]))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.audits()), 1)

    def test_concurrent_resolve_reopen_serializes_both_audits(self):
        self.configure()
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        ready = threading.Event()
        def resolve():
            with configuration_scope("tenant"), Session(self.engine) as session:
                incident, group = life.lock_incident(session, "tenant", identifier, self.actor())
                life.transition(session, incident, "resolved", at=self.origin + timedelta(seconds=1), group=group)
                ready.set()
                session.commit()
        event = self.save(self.event(), 2)
        def reopen():
            self.assertTrue(ready.wait(timeout=15))
            return self.correlate(event, save=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(resolve), pool.submit(reopen)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(len(self.audits()), 2)
        self.assertEqual(self.incidents()[0].lifecycle_context["flapping"]["transition_count"], 2)

    def test_actual_migration_retains_manual_fields_and_refuses_state_loss(self):
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        self.operator_state()
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE incident DROP COLUMN lifecycle_context"))
            connection.execute(text("ALTER TABLE incidentcorrelationgroup DROP COLUMN lifecycle_state"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-05-22-00_f38a21d67b04.py"
        spec = importlib.util.spec_from_file_location("lifecycle_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            self.assertEqual((row.assignee, row.user_summary), ("engineer@example.org", "Keep this note"))
            self.assertIsNone(row.lifecycle_context)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        self.correlate(self.event(status="resolved"), 1)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "Lifecycle state exists"):
                migration.downgrade()
