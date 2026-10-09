"""Real PostgreSQL locks/transactions, run only against an isolated test database."""

import importlib.util
import os
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from keep.api.bl.silences_bl import SilencesBL
from keep.api.bl.silences_delivery_bl import materialize_time_transitions
from keep.api.models.db.silence import NotificationDelivery, NotificationTransportBudget
from keep.api.models.silence import CancelSilenceCommand, utc_now, utc_string
from tests.silence_integration_fixtures import actor_token
from tests.test_silences_api_fork import NOW
from tests import test_silences_outbox_fork as outbox_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")


@unittest.skipUnless(DSN, "Requires the dedicated k3d PostgreSQL test sidecar")
class PostgresSilenceIntegrationTest(outbox_tests.SilenceOutboxTest):
    def setUp(self):
        schema = "silences_check_" + uuid4().hex
        admin = create_engine(DSN)
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        def cleanup():
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        self.addCleanup(cleanup)
        engine = create_engine(DSN, connect_args={"options": "-csearch_path=" + schema}, pool_size=5)
        self.enterContext(patch("tests.team_fork_test_case.create_engine", return_value=engine))
        super().setUp()

    def parallel(self, first, second):
        barrier = Barrier(2)
        def run(operation):
            barrier.wait(timeout=10)
            return operation()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(run, operation) for operation in (first, second)]
            return [job.result(timeout=20) for job in jobs]

    def test_two_clock_workers_publish_one_revision_and_one_fanout(self):
        self.create(self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                                 ends_at=utc_string(NOW + timedelta(seconds=20))))
        self.clock += timedelta(seconds=10)
        results = self.parallel(self.transition, self.transition)
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.counts(), (1, 1, 2))
        self.assertEqual(len(self.rows()), 4)

    def test_cancel_and_activation_serialize_without_duplicate_transition(self):
        original = self.create(self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                                            ends_at=utc_string(NOW + timedelta(seconds=20)))).result
        self.clock += timedelta(seconds=10)
        def cancel():
            with Session(self.engine) as session:
                try:
                    return SilencesBL(session, self.entity, self.clock).cancel(original.id,
                        CancelSilenceCommand(schema_version=1, client_request_id=uuid4(),
                            expected_revision=1, reason="Race", correlation_id=None))[1]
                except HTTPException as error:
                    return error.status_code
        result = self.parallel(self.transition, cancel)
        self.assertIn(result[1], (200, 409))
        types = [row.event_type for row in self.events()]
        self.assertIn(types, (["silence.created", "silence.cancelled"],
                              ["silence.created", "silence.activated"]))
        self.assertEqual(len(self.rows()), 4)

    def test_two_delivery_workers_do_not_send_the_same_live_lease(self):
        self.create()
        results = self.parallel(lambda: self.worker().run_once(), lambda: self.worker().run_once())
        self.assertEqual(sum(result["delivered"] for result in results), 2)
        self.assertEqual(len(self.receiver_a.events), 1)
        self.assertEqual(len(self.receiver_b.events), 1)
        self.assertTrue(all(row.state == "delivered" for row in self.rows()))

    def test_concurrent_duplicate_command_has_one_rule_audit_receipt_and_outbox(self):
        command = self.command()
        results = self.parallel(lambda: self.create(command), lambda: self.create(command))
        self.assertEqual(results[0].result.id, results[1].result.id)
        self.assertEqual(sum(result.replayed for result in results), 1)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(len(self.rows()), 2)

    def test_postgres_additive_migration_preserves_existing_silence_audit(self):
        self.create()
        path = Path(__file__).parents[1] / "keep/api/models/db/migrations/versions/2026-10-04-18-00_9d6a8f3b5e27.py"
        spec = importlib.util.spec_from_file_location("pg_outbox_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # These two new tables are confined to this test's private schema.
        NotificationTransportBudget.__table__.drop(self.engine)
        NotificationDelivery.__table__.drop(self.engine)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual({index["name"] for index in inspect(self.engine).get_indexes("notificationdelivery")},
                         {"ix_notificationdelivery_due", "ix_notificationdelivery_team", "uq_notificationdelivery_receiver"})

    def test_actual_api_startup_delivers_without_manual_worker_invocation(self):
        import keep.api.api as api
        with patch.dict(os.environ, {"KEEP_METRICS": "false", "KEEP_OTEL_ENABLED": "false"}), \
             patch.multiple(api, SCHEDULER=False, CONSUMER=False, TOPOLOGY=False, WATCHER=False,
                            MAINTENANCE_WINDOWS=False):
            app = api.get_app()
            with TestClient(app) as client:
                command = self.command(ends_at=utc_string(utc_now() + timedelta(minutes=5)))
                response = client.post("/integrations/silences", json=json.loads(command.json()),
                    headers={"X-API-KEY": self.service_keys["a"], "X-Keep-Actor-Token": actor_token(self)})
                self.assertEqual(response.status_code, 201)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    rows = self.rows()
                    if len(self.receiver_a.events) == len(self.receiver_b.events) == 1 and len(rows) == 2 and all(row.state == "delivered" for row in rows):
                        break
                    time.sleep(0.1)
                self.assertEqual(len(self.receiver_a.events), 1)
                self.assertEqual(len(self.receiver_b.events), 1)
                self.assertTrue(all(row.state == "delivered" for row in self.rows()))

    def test_lifespan_reads_configuration_with_worker_owned_connections(self):
        import asyncio
        import multiprocessing
        import keep.api.api as api
        from fastapi import FastAPI

        with self.engine.connect() as connection:
            parent_backend = connection.scalar(text("SELECT pg_backend_pid()"))
        original_config = api.process_silences_task.get_silence_integrations
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(4)
        results = context.Queue()

        def child():
            try:
                barrier.wait(timeout=10)
                def configuration():
                    with self.engine.connect() as connection:
                        backend = connection.scalar(text("SELECT pg_backend_pid()"))
                    if backend == parent_backend:
                        raise AssertionError("Configuration used the parent's database connection")
                    results.put(backend)
                    return original_config()

                async def run():
                    with patch.multiple(api, SCHEDULER=False, CONSUMER=False, TOPOLOGY=False,
                                        WATCHER=False, MAINTENANCE_WINDOWS=False), \
                         patch.object(api.process_silences_task, "get_silence_integrations", configuration):
                        async with api.lifespan(FastAPI()):
                            pass
                asyncio.run(run())
            except Exception as error:
                results.put(type(error).__name__)
                raise

        processes = [context.Process(target=child) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)
            self.assertEqual([process.exitcode for process in processes], [0] * 4)
            workers = [results.get(timeout=5) for _ in processes]
            self.assertEqual(len(set(workers)), 4)
            self.assertNotIn(parent_backend, workers)
            with self.engine.connect() as connection:
                self.assertEqual(connection.scalar(text("SELECT pg_backend_pid()")), parent_backend)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()
