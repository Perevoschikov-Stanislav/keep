"""Tests for Alertmanager active reconciliation, ghost alert resolution, and silence synchronization."""

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import celpy
from sqlmodel import Session, select

from keep.api.core.alertmanager_reconciliation import AlertmanagerReconciler, matchers_to_cel
from keep.api.bl.silences_bl import compile_filter
from keep.api.models.alert import AlertDto, AlertStatus
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.silence import Silence, NotificationDelivery
from tests.test_incident_core_verification_fork import IncidentCoreVerificationTest


class AlertmanagerReconciliationTest(IncidentCoreVerificationTest):
    def test_ghost_resolution_retains_core_root_fields_for_re_normalization(self):
        event = self.event("fp-root-projection", workload="catalog", team="alpha")
        event.family, event.svc, event.cluster, event.alertname = "workload", "catalog", "customer-a", "KubePodCrashLooping"
        event.service, event.description = "catalog", "Deployment catalog is failing"
        self.save(event)
        reconciler = self.reconciler(grace_period_seconds=1, consecutive_misses_required=1, seconds=200)
        with Session(self.engine) as session, patch("keep.api.core.alertmanager_reconciliation.process_event") as process:
            result = reconciler.reconcile_alerts(session, [{"fingerprint": "different"}])
        self.assertEqual(result["resolved"], [event.fingerprint])
        resolved = process.call_args.args[-1]
        self.assertEqual((resolved.family, resolved.svc, resolved.cluster, resolved.alertname),
                         ("workload", "catalog", "customer-a", "KubePodCrashLooping"))
        self.assertEqual(resolved.service, event.service)
        self.assertEqual(resolved.description, event.description)
        self.assertEqual(resolved.team_id, "alpha")
        self.assertEqual(resolved.status, "resolved")
        self.assertIsNone(resolved.event_id)
        self.assertNotEqual(resolved.id, event.id)

    def reconciler(self, **kwargs):
        seconds = kwargs.pop("seconds", 200)
        defaults = {
            "alertmanager_url": "http://fake-am:9093",
            "tenant_id": "tenant",
            "provider_types": ("prometheus", "alertmanager", "keep"),
            "clock": lambda: self.origin + timedelta(seconds=seconds),
        }
        defaults.update(kwargs)
        return AlertmanagerReconciler(**defaults)

    def test_matchers_to_cel_compilation_and_evaluation(self):
        """Matchers correctly compile into CEL with has() guards and evaluate properly."""
        matchers = [
            {"name": "alertname", "value": "HighCpu", "isRegex": False, "isEqual": True},
            {"name": "env", "value": "test", "isRegex": False, "isEqual": False},
            {"name": "service", "value": "auth-.*", "isRegex": True, "isEqual": True},
            {"name": "pod", "value": "canary-.*", "isRegex": True, "isEqual": False},
        ]
        cel_expr = matchers_to_cel(matchers)

        prg = compile_filter(cel_expr, alertmanager=True)

        # Matching event
        matching_labels = {
            "alertname": "HighCpu",
            "env": "prod",
            "service": "auth-service",
            "pod": "worker-1",
        }
        act_matching = celpy.json_to_cel({"labels": matching_labels})
        self.assertTrue(prg.evaluate(act_matching))

        # Missing alertname -> fails
        act_missing = celpy.json_to_cel({"labels": {"env": "prod", "service": "auth-svc"}})
        self.assertFalse(prg.evaluate(act_missing))

        # Equal env=="test" -> fails negative check
        act_bad_env = celpy.json_to_cel({"labels": {**matching_labels, "env": "test"}})
        self.assertFalse(prg.evaluate(act_bad_env))

        # Alertmanager rejects an empty matcher set; never widen it to true.
        with self.assertRaises(ValueError):
            matchers_to_cel([])

    def test_ghost_alert_reconciled_after_two_consecutive_misses(self):
        """An alert that disappeared from Alertmanager is resolved only after 2 consecutive misses."""
        # Ingest and save an active firing alert 200s ago
        event = self.event("fp-ghost-1", workload="payment", team="alpha", alertname="PaymentFailures")
        self.correlate(event)

        incidents_before = self.incidents()
        self.assertEqual(len(incidents_before), 1)
        self.assertEqual(incidents_before[0].status, "firing")

        with Session(self.engine) as session:
            # Verify it's firing in Keep
            row = session.exec(select(Alert).where(Alert.fingerprint == "fp-ghost-1")).first()
            self.assertEqual(row.event.get("status"), "firing")

        # Mock Alertmanager: returns a different active alert, but not fp-ghost-1
        am_alerts = [{"fingerprint": "fp-other", "status": {"state": "active"}}]

        reconciler = self.reconciler(grace_period_seconds=60, consecutive_misses_required=2, seconds=200)

        with Session(self.engine) as session:
            # Pass 1: 1st miss recorded, not resolved yet
            res1 = reconciler.reconcile_alerts(session, am_alerts)
            self.assertEqual(res1["misses_tracked"].get("fp-ghost-1"), 1)
            self.assertEqual(res1["resolved"], [])

            # Confirm still firing in DB
            alert_pass1 = session.exec(select(Alert).where(Alert.fingerprint == "fp-ghost-1")).all()
            self.assertEqual(alert_pass1[-1].event.get("status"), "firing")

            # Pass 2: 2nd consecutive miss -> triggers resolution
            res2 = reconciler.reconcile_alerts(session, am_alerts)
            self.assertEqual(res2["resolved"], ["fp-ghost-1"])

            # Verify in DB: alert is now resolved!
            session.expire_all()
            last_alert = session.exec(
                select(LastAlert).where(LastAlert.tenant_id == "tenant", LastAlert.fingerprint == "fp-ghost-1")
            ).first()
            resolved_row = session.get(Alert, last_alert.alert_id)
            self.assertEqual(resolved_row.event.get("status"), "resolved")
            self.assertEqual(resolved_row.event.get("resolved_by"), "alertmanager_reconciler")

        # Verify correlated incident is now automatically resolved!
        incidents_after = self.incidents()
        self.assertEqual(len(incidents_after), 1)
        self.assertEqual(incidents_after[0].status, "resolved")

    def test_reappearing_alert_resets_miss_counter(self):
        """If an alert reappears in Alertmanager before the 2nd miss, its miss counter resets."""
        event = self.event("fp-flaky", workload="auth", team="alpha")
        self.save(event, seconds=0)

        reconciler = self.reconciler(grace_period_seconds=30, consecutive_misses_required=2, seconds=100)

        with Session(self.engine) as session:
            # Pass 1: missing in AM
            res1 = reconciler.reconcile_alerts(session, [{"fingerprint": "other"}])
            self.assertEqual(res1["misses_tracked"].get("fp-flaky"), 1)

            # Pass 2: AM returns fp-flaky
            res2 = reconciler.reconcile_alerts(session, [{"fingerprint": "fp-flaky"}])
            self.assertEqual(res2["resolved"], [])
            self.assertNotIn("fp-flaky", reconciler.consecutive_misses)

    def test_grace_period_protects_fresh_alert(self):
        """Alerts within grace_period_seconds are not counted as misses."""
        event = self.event("fp-fresh", workload="fresh-svc", team="alpha")
        self.save(event, seconds=0)

        reconciler = self.reconciler(grace_period_seconds=180, consecutive_misses_required=2, seconds=30)

        with Session(self.engine) as session:
            res = reconciler.reconcile_alerts(session, [{"fingerprint": "other"}])
            self.assertIn("fp-fresh", res["skipped_grace"])
            self.assertNotIn("fp-fresh", res["misses_tracked"])
            self.assertEqual(res["resolved"], [])

    def test_circuit_breaker_empty_am_response_protects_keep(self):
        """If Alertmanager returns an empty list while Keep has active alerts, circuit breaker activates."""
        event = self.event("fp-active", workload="billing", team="alpha")
        self.save(event, seconds=0)

        reconciler = self.reconciler(grace_period_seconds=10, seconds=100)

        with Session(self.engine) as session:
            res = reconciler.reconcile_alerts(session, [])
            self.assertTrue(res["circuit_broken"])
            self.assertEqual(res["resolved"], [])

    def test_circuit_breaker_sharp_drop_protects_keep(self):
        """If Alertmanager alert count drops sharply by >50%, circuit breaker activates."""
        event = self.event("fp-drop-test", workload="orders", team="alpha")
        self.save(event, seconds=0)

        reconciler = self.reconciler(grace_period_seconds=10, drop_ratio_threshold=0.5, seconds=100)
        # Previous count was 10
        reconciler.last_seen_am_count = 10

        with Session(self.engine) as session:
            # AM returns only 3 alerts (< 50% of 10)
            res = reconciler.reconcile_alerts(session, [{"fingerprint": f"p-{i}"} for i in range(3)])
            self.assertTrue(res["circuit_broken"])
            self.assertEqual(res["resolved"], [])

    def test_silence_mirroring_and_cancellation(self):
        """Active Alertmanager silences are mirrored into Keep, and cancelled when removed."""
        reconciler = self.reconciler(seconds=10)

        am_silence = {
            "id": "am-silence-123",
            "status": {"state": "active"},
            "startsAt": "2026-10-05T12:00:00Z",
            "endsAt": "2026-10-05T14:00:00Z",
            "comment": "Scheduled maintenance",
            "createdBy": "ops-oncall",
            "matchers": [
                {"name": "alertname", "value": "ServiceDown", "isRegex": False, "isEqual": True},
                {"name": "env", "value": "prod", "isRegex": False, "isEqual": True},
            ],
        }

        with Session(self.engine) as session:
            # Pass 1: Active in AM -> Mirrored in Keep
            res1 = reconciler.reconcile_silences(session, [am_silence])
            self.assertTrue(len(res1["created"]) >= 1)

            silences = session.exec(
                select(Silence).where(Silence.origin == "alertmanager")
            ).all()
            self.assertTrue(len(silences) >= 1)
            silence = silences[0]
            self.assertTrue(silence.correlation_id.startswith("am-silence-123"))
            self.assertIsNone(silence.cancelled_at)
            self.assertIn("ServiceDown", silence.selector["cel"])

            # Pass 2: AM silence expired / removed -> Cancelled in Keep
            res2 = reconciler.reconcile_silences(session, [])
            self.assertTrue(len(res2["cancelled"]) >= 1)

            session.refresh(silence)
            self.assertIsNotNone(silence.cancelled_at)

    def test_mirrored_silence_suppresses_incident_notifications(self):
        """A mirrored Alertmanager silence suppresses reminder/notification dispatching in Keep."""
        # 1. Create and correlate alert for team alpha
        event = self.event("fp-suppressed", workload="catalog", team="alpha", alertname="HighErrorRate")
        self.correlate(event)

        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)

        # 2. Mirror an AM silence matching HighErrorRate
        reconciler = self.reconciler(seconds=0)
        am_silence = {
            "id": "am-silence-suppress",
            "status": {"state": "active"},
            "startsAt": "2026-10-05T11:00:00Z",
            "endsAt": "2026-10-05T13:00:00Z",
            "comment": "Night silence",
            "createdBy": "sre",
            "matchers": [
                {"name": "alertname", "value": "HighErrorRate", "isRegex": False, "isEqual": True},
            ],
        }
        with Session(self.engine) as session:
            reconciler.reconcile_silences(session, [am_silence])

        # 3. Run notification dispatcher
        self.dispatcher().run_once()

        # Under silence: delivery state is skipped with last_error_code="silenced", no messages sent!
        self.assertEqual(len(self.sent), 0)

        with Session(self.engine) as session:
            deliveries = session.exec(select(NotificationDelivery)).all()
            self.assertTrue(len(deliveries) >= 1)
            for d in deliveries:
                self.assertEqual(d.state, "skipped")
                self.assertEqual(d.last_error_code, "silenced")
