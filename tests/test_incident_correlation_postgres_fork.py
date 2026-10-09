"""PostgreSQL concurrency and additive correlation migration, only in k3d."""

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

from keep.api.models.db.alert import Alert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.rule import Rule
from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
from keep.rulesengine.rulesengine import RulesEngine
from tests import test_incident_correlation_fork as correlation_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    url = make_url(DSN)
    if url.host != "127.0.0.1" or url.database != "silences_check":
        raise RuntimeError("Correlation tests require the private loopback PostgreSQL sidecar")


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentCorrelationTest(correlation_tests.IncidentCorrelationTest):
    def setUp(self):
        schema = "correlation_check_" + uuid4().hex
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

    def test_same_key_concurrency_creates_one_incident_and_all_links(self):
        events = [self.save(self.event("p-" + str(index))) for index in range(8)]
        self.concurrent(events)
        rows = self.incidents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].alerts_count, 8)
        self.assertEqual(sum(call.args[-1] == "created" for call in self.published.call_args_list), 1)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(LastAlertToIncident)).all()), 8)
            self.assertEqual(len(session.exec(select(IncidentCorrelationGroup)).all()), 1)

    def test_same_saved_version_concurrency_correlates_exactly_once(self):
        event = self.save(self.event())
        self.concurrent([event.copy(deep=True) for _ in range(8)])
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0].alerts_count, 1)
        self.assertEqual(self.published.call_count, 1)

    def test_different_keys_concurrency_allocates_unique_running_numbers(self):
        events = [self.save(self.event("p-" + str(index), workload="w-" + str(index))) for index in range(8)]
        self.concurrent(events)
        rows = self.incidents()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row.running_number for row in rows}), 8)
        self.assertTrue(all(row.running_number is not None for row in rows))

    def test_parallel_keys_concurrency_has_no_deadlock_or_duplicate_group(self):
        self.second_rule()
        self.bundle["correlation_overlap"] = "parallel"
        self.apply()
        events = [self.save(self.event("p-" + str(index), service="catalog")) for index in range(6)]
        self.concurrent(events)
        self.assertEqual(len(self.incidents()), 2)
        self.assertTrue(all(row.alerts_count == 6 for row in self.incidents()))

    def test_parallel_shared_and_different_keys_follow_one_lock_order(self):
        self.second_rule()
        self.bundle["correlation_overlap"] = "parallel"
        self.apply()
        events = [self.save(self.event("p-" + str(index), workload="w-" + str(index), service="catalog")) for index in range(6)]
        self.concurrent(events)
        self.assertEqual(len(self.incidents()), 7)
        shared = next(row for row in self.incidents() if row.correlation_context["rule_id"] == "service")
        self.assertEqual(shared.alerts_count, 6)

    def test_actual_migration_preserves_old_records_and_refuses_history_loss(self):
        self.save(self.event())
        with Session(self.engine) as session:
            session.add(Incident(tenant_id="tenant", team_id="alpha", user_generated_name="Manual", user_summary="Notes", assignee="engineer"))
            session.commit()
            identifier = session.exec(select(Incident.id)).one()
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE incidentcorrelationgroup"))
            connection.execute(text("ALTER TABLE incident DROP COLUMN correlation_context"))
            connection.execute(text("ALTER TABLE alert DROP COLUMN correlation_context"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-05-21-00_e5d04b9ca716.py"
        spec = importlib.util.spec_from_file_location("correlation_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            self.assertEqual((row.user_generated_name, row.user_summary, row.assignee), ("Manual", "Notes", "engineer"))
            self.assertIsNone(row.correlation_context)
            self.assertEqual(len(session.exec(select(Alert)).all()), 1)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
            # Test the correlation migration itself, then retain the newer nullable
            # lifecycle column required by the current model.
            connection.execute(text("ALTER TABLE incidentcorrelationgroup ADD COLUMN lifecycle_state JSON"))
        self.correlate(self.event("p-2"))
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "Correlation history exists"):
                migration.downgrade()

    def test_legacy_rule_same_key_concurrency_creates_one_incident(self):
        from datetime import datetime
        with Session(self.engine) as session:
            rule = Rule(tenant_id="tenant", name="Legacy", created_by="fixture", creation_time=datetime.utcnow(),
                        definition={"sql": "1=1", "params": {}}, definition_cel="true", timeframe=600,
                        grouping_criteria=["normalized.workload"])
            session.add(rule)
            session.commit()
            rule_id = rule.id
        events = [self.save(self.event("legacy-" + str(index))) for index in range(8)]
        barrier = threading.Barrier(8)
        def run(event):
            with Session(self.engine) as session:
                rule = session.get(Rule, rule_id)
                engine = RulesEngine("tenant")
                key = engine._calc_rule_fingerprint(event, rule)[0][0]
                barrier.wait(timeout=15)
                return engine._get_or_create_incident(rule, key, session, event)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, events))
        self.assertEqual(len({incident.id for incident, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len(self.incidents()), 1)

    def test_manual_and_correlated_incidents_share_running_number_allocation(self):
        events = [self.save(self.event("p-" + str(index), workload="w-" + str(index))) for index in range(4)]
        barrier = threading.Barrier(8)
        def run(index):
            with Session(self.engine) as session:
                barrier.wait(timeout=15)
                if index < 4:
                    return RulesEngine("tenant").run_rules([events[index]], session)
                row = Incident(tenant_id="tenant", team_id="alpha", user_generated_name="Manual " + str(index))
                session.add(row)
                session.commit()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, range(8)))
        rows = self.incidents()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row.running_number for row in rows}), 8)
