"""Atomic audit/outbox, restart recovery, fan-out and clock boundaries."""

import importlib.util
import io
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, text
from sqlmodel import Session, select

from keep.api.bl.silences_bl import SilencesBL
from keep.api.bl.silences_delivery_bl import SilenceDeliveryWorker, materialize_time_transitions
from keep.api.models.db.silence import NotificationDelivery, Silence, SilenceEvent
from keep.api.models.silence import CancelSilenceCommand, UpdateSilenceCommand, utc_string
from tests.silence_integration_fixtures import configure_integrations
from tests.test_silences_api_fork import NOW, SilenceDatabaseCase


class SilenceOutboxTest(SilenceDatabaseCase):
    def setUp(self):
        super().setUp()
        configure_integrations(self)
        self.clock = NOW

    def worker(self, **kwargs):
        return SilenceDeliveryWorker(self.engine, self.settings, clock=lambda: self.clock, **kwargs)

    def rows(self):
        with Session(self.engine) as session:
            return session.exec(select(NotificationDelivery).order_by(NotificationDelivery.created_at,
                NotificationDelivery.id)).all()

    def events(self):
        with Session(self.engine) as session:
            return session.exec(select(SilenceEvent).order_by(SilenceEvent.revision)).all()

    def transition(self):
        with Session(self.engine) as session:
            return materialize_time_transitions(session, now=self.clock, settings=self.settings)

    def test_rule_receipt_audit_and_two_deliveries_rollback_as_one_transaction(self):
        def reject(session, *args):
            if any(isinstance(row, NotificationDelivery) for row in session.new):
                raise RuntimeError("Synthetic outbox failure")
        event.listen(Session, "before_flush", reject)
        try:
            with self.assertRaisesRegex(RuntimeError, "Synthetic outbox failure"):
                self.create()
        finally:
            event.remove(Session, "before_flush", reject)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.receiver_a.events + self.receiver_b.events, [])

    def test_create_commits_without_network_and_new_worker_recovers_after_restart(self):
        with patch("keep.api.bl.silences_delivery_bl.requests.post", side_effect=AssertionError("No HTTP during command")):
            self.create()
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row.state == "pending" for row in self.rows()))
        result = self.worker().run_once()
        self.assertEqual(result["delivered"], 2)
        self.assertTrue(all(row.state == "delivered" for row in self.rows()))
        self.assertEqual(len(self.receiver_a.events), 1)
        self.assertEqual(len(self.receiver_b.events), 1)
        self.assertEqual(self.receiver_a.events[0]["event_id"], self.receiver_b.events[0]["event_id"])
        self.assertTrue(self.receiver_b.events[0]["authorization"] == "Bearer " + self.outgoing_key)
        self.assertTrue(all(value not in json.dumps(row.payload) for row in self.rows()
                            for value in (*self.service_keys.values(), self.outgoing_key)))

    def test_unavailable_receiver_does_not_block_other_delivery_or_change_the_rule(self):
        created = self.create()
        self.receiver_a.status = 503
        self.assertEqual(self.worker().run_once()["delivered"], 1)
        rows = {row.transport_id: row for row in self.rows()}
        self.assertEqual(rows["receiver-a"].state, "pending")
        self.assertEqual(rows["receiver-a"].last_error_code, "http_503")
        self.assertEqual(rows["receiver-b"].state, "delivered")
        self.clock += timedelta(seconds=2)
        self.receiver_a.status = 200
        self.assertEqual(self.worker().run_once()["delivered"], 1)
        self.assertEqual(len(self.receiver_b.events), 1)
        self.assertEqual(self.receiver_a.events[0]["payload"], self.receiver_a.events[1]["payload"])
        with Session(self.engine) as session:
            self.assertEqual(session.get(Silence, created.result.id).revision, 1)

    def test_retry_limit_keeps_diagnostic_and_audit_instead_of_losing_event(self):
        self.create()
        self.receiver_a.status = 503
        for delay in (0, 1, 2):
            self.clock += timedelta(seconds=delay)
            self.worker().run_once()
        failed = next(row for row in self.rows() if row.transport_id == "receiver-a")
        self.assertEqual((failed.state, failed.attempts, failed.last_error_code), ("failed", 3, "http_503"))
        self.clock += timedelta(minutes=1)
        self.worker().run_once()
        self.assertEqual(len(self.receiver_a.events), 3)
        self.assertEqual(len(self.events()), 1)

    def test_expired_lease_recovers_and_old_worker_cannot_send_after_losing_ownership(self):
        self.create()
        first_worker = self.worker()
        claimed = first_worker.claim()
        self.assertIsNotNone(claimed)
        self.clock += timedelta(seconds=6)
        recovered = self.worker().claim()
        self.assertEqual(recovered.id, claimed.id)
        self.assertNotEqual(recovered.lease_token, claimed.lease_token)
        self.assertFalse(first_worker.send_claimed(claimed))
        self.assertEqual(self.receiver_a.events + self.receiver_b.events, [])
        self.assertTrue(self.worker().send_claimed(recovered))
        self.assertEqual(len(self.receiver_a.events) + len(self.receiver_b.events), 1)

    def test_unexpired_lease_prevents_double_claim(self):
        self.create()
        first, second = self.worker().claim(), self.worker().claim()
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(self.worker().claim())
        self.assertTrue(self.worker().send_claimed(first))
        self.assertTrue(self.worker().send_claimed(second))

    def test_crash_after_receiver_commit_recovers_with_the_same_event_and_delivery_id(self):
        self.create()
        worker = self.worker()
        claimed = worker.claim()
        def send_then_crash(delivery, transport):
            worker._send(delivery, transport)
            raise RuntimeError("Synthetic crash after receiver commit")
        worker.sender = send_then_crash
        with self.assertRaisesRegex(RuntimeError, "Synthetic crash"):
            worker.send_claimed(claimed)
        self.clock += timedelta(seconds=6)
        recovered = self.worker().claim()
        self.assertEqual(recovered.id, claimed.id)
        self.assertTrue(self.worker().send_claimed(recovered))
        entries = [entry for entry in self.receiver_a.events + self.receiver_b.events
                   if entry["delivery_id"] == str(claimed.id)]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["payload"], entries[1]["payload"])
        receiver = self.receiver_a if claimed.transport_id == "receiver-a" else self.receiver_b
        self.assertEqual(len(receiver.projected_events), 1)

    def test_redirect_is_a_retryable_failure_and_does_not_forward_credentials(self):
        self.create()
        self.receiver_b.status = 307
        self.receiver_b.redirect_url = self.receiver_a.url + "/untrusted-target"
        self.assertEqual(self.worker().run_once()["delivered"], 1)
        self.assertEqual(len(self.receiver_a.events), 1)
        self.assertIsNone(self.receiver_a.events[0]["authorization"])
        failed = next(row for row in self.rows() if row.transport_id == "receiver-b")
        self.assertEqual((failed.state, failed.last_error_code), ("pending", "http_307"))

    def test_receiver_ignores_older_and_duplicate_events_and_snapshot_restores_state(self):
        created = self.create().result
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, NOW).update(created.id, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"comment": "Updated"}, correlation_id=None))
            SilencesBL(session, self.entity, NOW).cancel(created.id, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=2,
                reason="Done", correlation_id=None))
        deliveries = sorted([row for row in self.rows() if row.transport_id == "receiver-a"],
                            key=lambda row: row.payload["revision"], reverse=True)
        worker = self.worker()
        for row in [*deliveries, deliveries[0]]:
            worker._send(row, self.settings.transports[row.transport_id])
        projection = self.receiver_a.projection[str(created.id)]
        self.assertEqual((projection["revision"], projection["state"]), (3, "cancelled"))
        with Session(self.engine) as session:
            snapshot = SilencesBL(session, self.entity, NOW).get(created.id)
        self.assertEqual(projection, json.loads(snapshot.json()))

    def test_rate_budget_is_shared_between_workers(self):
        self.settings.transports["receiver-a"]["delivery"]["rate_limit"] = {"per_second": 1, "burst": 1}
        self.create()
        self.create()
        self.assertEqual(self.worker().run_once()["delivered"], 3)
        self.assertEqual(len(self.receiver_a.events), 1)
        self.assertEqual(len(self.receiver_b.events), 2)
        self.clock += timedelta(seconds=1)
        self.assertEqual(self.worker().run_once()["delivered"], 1)
        self.assertEqual(len(self.receiver_a.events), 2)

    def test_disabled_subscription_stops_pending_send_without_removing_history(self):
        self.create()
        del self.settings.subscribers["watch-alpha-a"]
        self.assertEqual(self.worker().run_once()["delivered"], 1)
        disabled = next(row for row in self.rows() if row.transport_id == "receiver-a")
        self.assertEqual((disabled.state, disabled.last_error_code), ("disabled", "subscription_disabled"))
        self.assertEqual(self.receiver_a.events, [])
        self.assertEqual(len(self.events()), 1)

    def test_exact_activation_expiry_and_no_debounce_of_lifecycle(self):
        command = self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                               ends_at=utc_string(NOW + timedelta(seconds=20)))
        result = self.create(command)
        self.clock += timedelta(seconds=10)
        self.assertEqual(self.transition(), 1)
        self.assertEqual(self.transition(), 0)
        activation = self.events()[1]
        self.assertEqual(activation.payload["effective_at"], command.starts_at)
        self.assertEqual((activation.revision, activation.payload["resource"]["state"]), (2, "active"))
        self.clock += timedelta(seconds=10)
        with Session(self.engine) as session:
            before_scan = SilencesBL(session, self.entity, self.clock).get(result.result.id)
            self.assertEqual((before_scan.revision, before_scan.state), (2, "expired"))
        self.assertEqual(self.transition(), 1)
        expiry = self.events()[2]
        self.assertEqual(expiry.payload["effective_at"], command.ends_at)
        self.assertEqual(expiry.revision, 3)
        self.assertEqual(expiry.payload["actor"], expiry.payload["resource"]["updated_by"])
        self.assertEqual(expiry.payload["origin"], "keep-system")
        self.assertIsNone(expiry.payload["client_request_id"])
        self.assertEqual(self.worker().run_once()["delivered"], 6)
        self.assertEqual(len(self.receiver_a.events), 3)
        self.assertEqual(len(self.receiver_b.events), 3)

    def test_missed_whole_window_emits_expiry_only(self):
        self.create(self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                                 ends_at=utc_string(NOW + timedelta(seconds=20))))
        self.clock += timedelta(seconds=30)
        self.assertEqual(self.transition(), 1)
        self.assertEqual([row.event_type for row in self.events()], ["silence.created", "silence.expired"])

    def test_updated_dates_and_cancellation_do_not_publish_stale_timers(self):
        original = self.create(self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                                            ends_at=utc_string(NOW + timedelta(seconds=20)))).result
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, NOW + timedelta(seconds=5)).update(original.id,
                UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
                    changes={"starts_at": utc_string(NOW + timedelta(seconds=30)),
                             "ends_at": utc_string(NOW + timedelta(seconds=40))}, correlation_id=None))
        self.clock += timedelta(seconds=21)
        self.assertEqual(self.transition(), 0)
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, self.clock).cancel(original.id,
                CancelSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=2,
                    reason="Cancelled", correlation_id=None))
        self.clock += timedelta(seconds=30)
        self.assertEqual(self.transition(), 0)
        self.assertEqual([row.event_type for row in self.events()],
                         ["silence.created", "silence.updated", "silence.cancelled"])

    def test_clock_transition_conflicts_with_old_operator_revision(self):
        original = self.create(self.command(starts_at=utc_string(NOW + timedelta(seconds=10)),
                                            ends_at=utc_string(NOW + timedelta(seconds=20)))).result
        self.clock += timedelta(seconds=10)
        self.transition()
        with Session(self.engine) as session, self.assertRaises(HTTPException) as failure:
            SilencesBL(session, self.entity, self.clock).cancel(original.id,
                CancelSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
                                     reason="Old revision", correlation_id=None))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(len(self.events()), 2)

    def test_lifecycle_is_delivered_while_the_target_is_silenced(self):
        self.create()
        self.assertTrue(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        self.assertEqual(self.worker().run_once()["delivered"], 2)
        original = self.events()[0].silence_id
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, NOW).cancel(original, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1, reason="Done", correlation_id=None))
        self.assertEqual(self.worker().run_once()["delivered"], 2)
        self.assertEqual({entry["payload"]["event_type"] for entry in self.receiver_a.events},
                         {"silence.created", "silence.cancelled"})


class NotificationOutboxMigrationTest(SilenceDatabaseCase):
    def module(self):
        path = Path(__file__).parents[1] / "keep/api/models/db/migrations/versions/2026-10-04-18-00_9d6a8f3b5e27.py"
        spec = importlib.util.spec_from_file_location("outbox_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_additive_migration_and_empty_downgrade_preserve_old_tables(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tenant (id VARCHAR(256) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE silence (id VARCHAR(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO silence VALUES ('preserved')"))
            with Operations.context(MigrationContext.configure(connection)):
                self.module().upgrade()
            self.assertIn("notificationdelivery", inspect(connection).get_table_names())
            with Operations.context(MigrationContext.configure(connection)):
                self.module().downgrade()
            self.assertNotIn("notificationdelivery", inspect(connection).get_table_names())
            self.assertEqual(connection.execute(text("SELECT id FROM silence")).scalar(), "preserved")

    def test_nonempty_downgrade_and_offline_downgrade_are_refused(self):
        self.create()
        with Session(self.engine) as session:
            source = session.exec(select(SilenceEvent)).one()
            session.add(NotificationDelivery(tenant_id="keep", team_id="alpha", event_id=source.event_id,
                subscriber_id="receiver", destination_id="destination", transport_id="transport",
                policy_digest="a" * 64, payload=source.payload, available_at=NOW, created_at=NOW))
            session.commit()
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "delivery history exists"):
                self.module().downgrade()
        self.assertIn("notificationdelivery", inspect(self.engine).get_table_names())
        for dialect in ("postgresql", "mysql", "mssql"):
            output = io.StringIO()
            context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": output})
            with Operations.context(context):
                self.module().upgrade()
                with self.assertRaisesRegex(RuntimeError, "Offline downgrade"):
                    self.module().downgrade()
            self.assertIn("uq_notificationdelivery_receiver", output.getvalue())
