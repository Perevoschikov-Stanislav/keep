"""Regression cases for the October 7 Alertmanager review."""

import copy
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import celpy
from fastapi import HTTPException
from sqlmodel import Session, select
import requests

from keep.api.bl.silences_bl import SilencesBL
from keep.api.bl.silences_bl import compile_filter
from keep.api.core.alertmanager_reconciliation import AlertmanagerReconciler, am_time, matchers_to_cel
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.silence import Silence, SilenceEvent, NotificationDelivery, AlertmanagerReconciliationState
from keep.api.models.silence import CreateSilenceCommand, CancelSilenceCommand, UpdateSilenceCommand, utc_string
from tests import test_incident_notifications_fork as fixtures


class AlertmanagerSyncTest(fixtures.NotificationCase):
    def setUp(self):
        super().setUp()
        self.clock = self.origin + timedelta(seconds=200)
        self.http = MagicMock()
        self.remote = []
        self.http.post.side_effect = self.post
        self.http.delete.side_effect = self.delete

    def delete(self, url, **kwargs):
        identifier = url.rsplit("/", 1)[-1]
        for row in self.remote:
            if row["id"] == identifier:
                row["status"] = {"state": "expired"}
        return MagicMock(status_code=200)

    def post(self, url, **kwargs):
        payload = copy.deepcopy(kwargs["json"])
        identifier = payload.pop("id", None) or str(uuid4())
        self.remote = [row for row in self.remote if row["id"] != identifier]
        self.remote.append({**payload, "id": identifier, "status": {"state": "active"}})
        result = MagicMock(status_code=200)
        result.json.return_value = {"silenceID": identifier}
        return result

    def am(self, **changes):
        result = {"id": str(uuid4()), "status": {"state": "active"},
            "startsAt": "2026-10-05T11:00:00Z", "endsAt": "2026-10-05T14:00:00Z",
            "createdBy": "operator", "comment": "review fixture",
            "matchers": [{"name": "alertname", "value": "HighCpu", "isEqual": True, "isRegex": False}]}
        result.update(changes)
        return result

    def reconciler(self, **kwargs):
        kwargs.setdefault("team_matchers", {"alpha": [[{"name": "zone", "value": "ALPHA"}]],
                                            "beta": [[{"name": "zone", "value": "BETA"}]]})
        return AlertmanagerReconciler("http://fake-am", tenant_id="tenant", provider_types=("keep", "prometheus"),
            grace_period_seconds=10, clock=lambda: self.clock, http_client=self.http, **kwargs)

    def save(self, event, seconds=0):
        super().save(event, seconds)
        from keep.api.core import db
        with Session(self.engine) as session:
            row = session.get(Alert, UUID(event.id))
            db.set_last_alert("tenant", row, session=session)
            session.commit()
        return event

    def native(self, session, *, correlation_id=None, selector=None):
        command = CreateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
            "team_id": "alpha", "selector": selector or {"kind": "filter", "cel": 'labels["alertname"] == "HighCpu"'},
            "starts_at": None, "ends_at": "2026-10-05T13:00:00Z", "comment": "review", "correlation_id": correlation_id})
        return SilencesBL(session, self.actor(), now=self.origin).create(command)[0].result.id

    def test_imported_silence_list_serializes_valid_service_actors(self):
        with Session(self.engine) as session:
            self.reconciler().reconcile_silences(session, [self.am()])
            rows = SilencesBL(session, self.actor(role="admin"), now=self.clock).list().items
            self.assertTrue(rows)
            self.assertEqual(rows[0].created_by.kind, "service")

    def test_exact_alert_export_preserves_source_labels_and_team(self):
        self.save(self.event("alpha-fp", alertname="HighCpu", zone="ALPHA", namespace="alpha-ns", instance="pod-a"))
        rec = self.reconciler()
        with Session(self.engine) as session:
            self.native(session, selector={"kind": "alert", "fingerprints": ["alpha-fp"]})
            rec.sync_keep_silences_to_am(session, [])
        matchers = self.http.post.call_args.kwargs["json"]["matchers"]
        values = {row["name"]: row["value"] for row in matchers}
        self.assertEqual(values["alertname"], "HighCpu")
        self.assertEqual(values["zone"], "ALPHA")
        self.assertEqual(values["namespace"], "alpha-ns")
        self.assertEqual(values["instance"], "pod-a")

    def test_user_correlation_cannot_delete_unrelated_am_silence(self):
        with Session(self.engine) as session:
            identifier = self.native(session, correlation_id="am_synced:unrelated-beta-id")
            SilencesBL(session, self.actor(), now=self.clock).cancel(identifier, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1, reason="review", correlation_id=None))
            self.reconciler().sync_keep_silences_to_am(session, [self.am(id="unrelated-beta-id")])
        self.http.delete.assert_not_called()

    def test_native_extension_updates_existing_external_silence(self):
        with Session(self.engine) as session:
            identifier = self.native(session)
            self.reconciler().sync_keep_silences_to_am(session, [])
            external_id = self.remote[0]["id"]
            SilencesBL(session, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"ends_at": "2026-10-05T15:00:00Z"}, correlation_id=None))
            self.reconciler().sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(self.remote[0]["id"], external_id)
        self.assertEqual(self.remote[0]["endsAt"], "2026-10-05T15:00:00Z")

    def test_imported_rules_reject_local_mutation_and_keep_cancelled_tombstones(self):
        rec = self.reconciler()
        remote = self.am()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [remote])
            row = session.exec(select(Silence).where(Silence.origin == "alertmanager")).first()
            with self.assertRaises(HTTPException) as error:
                SilencesBL(session, self.actor(role="admin"), now=self.clock).cancel(row.id, CancelSilenceCommand(
                    schema_version=1, client_request_id=uuid4(), expected_revision=row.revision,
                    reason="review", correlation_id=None))
            self.assertEqual(error.exception.status_code, 409)
            row.cancelled_at = self.clock
            session.add(row)
            session.commit()
            rec.reconcile_silences(session, [remote])
            session.refresh(row)
            self.assertIsNotNone(row.cancelled_at)

    def test_imported_changes_publish_lifecycle_events(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [self.am()])
            rec.reconcile_silences(session, [])
            rows = session.exec(select(SilenceEvent)).all()
            types = [row.event_type for row in rows if row.team_id == "alpha"]
            self.assertEqual(types, ["silence.created", "silence.cancelled"])

    def test_unsupported_filter_stays_local_with_explicit_status(self):
        with Session(self.engine) as session:
            identifier = self.native(session, selector={"kind": "filter", "cel": 'labels["alertname"] == "HighCpu" && !(labels["namespace"] == "unsafe")'})
            self.reconciler().sync_keep_silences_to_am(session, [])
            public = SilencesBL(session, self.actor(), now=self.clock).get(identifier)
            self.assertEqual(public.synchronization[0]["reason"], "unsupported_filter")
            self.assertEqual(public.synchronization[0]["state"], "local_only")
        self.http.post.assert_not_called()

    def test_regex_import_is_anchored_and_missing_labels_mean_empty_string(self):
        env = celpy.Environment()
        expression = matchers_to_cel([{"name": "service", "value": "auth", "isRegex": True, "isEqual": True}])
        self.assertFalse(compile_filter(expression, alertmanager=True).evaluate(celpy.json_to_cel({"labels": {"service": "oauth-proxy"}})))
        expression = matchers_to_cel([{"name": "absent", "value": "", "isRegex": False, "isEqual": True}])
        self.assertTrue(env.program(env.compile(expression)).evaluate(celpy.json_to_cel({"labels": {}})))

    def test_circuit_recovers_after_stable_lower_count_and_empty_snapshot(self):
        self.save(self.event("ghost"))
        rec = self.reconciler()
        rec.last_seen_am_count = 10
        with Session(self.engine) as session, patch("keep.api.core.alertmanager_reconciliation.process_event") as process:
            self.assertTrue(rec.reconcile_alerts(session, [{"fingerprint": "other"}])["circuit_broken"])
            self.clock += timedelta(seconds=400)
            rec.reconcile_alerts(session, [{"fingerprint": "other"}])
            self.clock += timedelta(seconds=60)
            self.assertEqual(rec.reconcile_alerts(session, [{"fingerprint": "other"}])["resolved"], ["ghost"])
            self.assertEqual(process.call_count, 1)

    def test_overlapping_workers_export_one_remote_silence(self):
        first, second = self.reconciler(), self.reconciler()
        response = self.http.post.side_effect
        def overlap(*args, **kwargs):
            with Session(self.engine) as other:
                second.sync_keep_silences_to_am(other, [])
            return response(*args, **kwargs)
        self.http.post.side_effect = overlap
        with Session(self.engine) as session:
            self.native(session)
            first.sync_keep_silences_to_am(session, [])
        self.assertEqual(self.http.post.call_count, 1)

    def test_unconfigured_team_never_exports_a_global_rule(self):
        with Session(self.engine) as session:
            identifier = self.native(session)
            self.reconciler(team_matchers={}).sync_keep_silences_to_am(session, [])
            status = SilencesBL(session, self.actor(), now=self.clock).get(identifier).synchronization[0]
            self.assertEqual(status["reason"], "team_scope_unconfigured")
        self.http.post.assert_not_called()

    def test_exact_multiple_alerts_do_not_form_a_label_cross_product(self):
        for fp, instance, cluster in (("first", "pod-a", "one"), ("second", "pod-b", "two")):
            self.save(self.event(fp, alertname="HighCpu", zone="ALPHA", instance=instance, cluster=cluster))
        with Session(self.engine) as session:
            self.native(session, selector={"kind": "alert", "fingerprints": ["first", "second"]})
            self.reconciler().sync_keep_silences_to_am(session, [])
        pairs = {(dict((m["name"], m["value"]) for m in row["matchers"])["instance"],
                  dict((m["name"], m["value"]) for m in row["matchers"])["cluster"]) for row in self.remote}
        self.assertEqual(pairs, {("pod-a", "one"), ("pod-b", "two")})

    def test_incident_export_uses_only_its_exact_member_labels(self):
        for fp, instance in (("one", "pod-a"), ("two", "pod-b")):
            self.correlate(self.event(fp, alertname="HighCpu", zone="ALPHA", instance=instance))
        with Session(self.engine) as session:
            self.native(session, selector={"kind": "incident", "incident_ids": [str(self.incidents()[0].id)]})
            self.reconciler().sync_keep_silences_to_am(session, [])
        self.assertEqual(self.http.post.call_count, 2)
        for row in self.remote:
            self.assertIn({"name": "zone", "value": "ALPHA", "isRegex": False, "isEqual": True}, row["matchers"])

    def test_loss_of_create_response_recovers_without_second_post(self):
        rec = self.reconciler()
        def accepted_then_lost(*args, **kwargs):
            self.post(*args, **kwargs)
            raise requests.ReadTimeout("response lost")
        self.http.post.side_effect = accepted_then_lost
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            self.assertEqual(SilencesBL(session, self.actor(), now=self.clock).get(identifier).synchronization[0]["state"], "uncertain")
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            self.assertEqual(SilencesBL(session, self.actor(), now=self.clock).get(identifier).synchronization[0]["state"], "synced")
        self.assertEqual(self.http.post.call_count, 1)
        self.assertEqual(len(self.remote), 1)

    def test_unknown_create_result_never_retries_blindly(self):
        self.http.post.side_effect = requests.ReadTimeout("unknown outcome")
        with Session(self.engine) as session:
            self.native(session)
            self.reconciler().sync_keep_silences_to_am(session, [])
            self.reconciler().sync_keep_silences_to_am(session, [])
        self.assertEqual(self.http.post.call_count, 1)

    def test_am_clamped_start_recovers_and_is_preserved_on_extension(self):
        rec = self.reconciler()
        def clamp_then_lose(*args, **kwargs):
            response = self.post(*args, **kwargs)
            self.remote[-1]["startsAt"] = "2026-10-05T12:03:20.123Z"
            raise requests.ReadTimeout("accepted but acknowledgement lost")
        self.http.post.side_effect = clamp_then_lose
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            self.assertEqual(self.http.post.call_count, 1)
            self.http.post.side_effect = self.post
            SilencesBL(session, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"ends_at": "2026-10-05T15:00:00Z"}, correlation_id=None))
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
        self.assertEqual(self.remote[0]["startsAt"], "2026-10-05T12:03:20.123Z")
        self.assertEqual(self.remote[0]["endsAt"], "2026-10-05T15:00:00Z")
        self.assertEqual(len(self.remote), 1)

    def round_remote_timestamps(self):
        # strfmt.DateTime serializes Alertmanager GET timestamps to milliseconds.
        for field in ("startsAt", "endsAt"):
            value = am_time(self.remote[-1][field])
            self.remote[-1][field] = utc_string(value.replace(microsecond=value.microsecond // 1000 * 1000))

    def test_lost_scheduled_create_recovers_millisecond_snapshot(self):
        rec = self.reconciler()
        def accepted_then_lost(*args, **kwargs):
            self.post(*args, **kwargs)
            self.round_remote_timestamps()
            raise requests.ReadTimeout("accepted but acknowledgement lost")
        self.http.post.side_effect = accepted_then_lost
        with Session(self.engine) as session:
            identifier = self.native(session)
            row = session.get(Silence, identifier)
            row.starts_at = (self.clock + timedelta(minutes=5)).replace(microsecond=123456)
            row.ends_at = row.ends_at.replace(microsecond=654321)
            session.add(row)
            session.commit()
            rec.sync_keep_silences_to_am(session, [])
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            row = session.get(Silence, identifier)
            self.assertEqual(row.external_context[rec.source_id]["state"], "synced")
            self.assertEqual(row.starts_at.microsecond, 123456)
            self.assertEqual(row.ends_at.microsecond, 654321)
        self.assertEqual(self.http.post.call_count, 1)

    def test_lost_clamped_create_recovers_millisecond_deadline(self):
        rec = self.reconciler()
        def accepted_then_lost(*args, **kwargs):
            self.post(*args, **kwargs)
            self.remote[-1]["startsAt"] = utc_string(self.clock.replace(microsecond=123456))
            self.round_remote_timestamps()
            raise requests.ReadTimeout("accepted but acknowledgement lost")
        self.http.post.side_effect = accepted_then_lost
        with Session(self.engine) as session:
            identifier = self.native(session)
            row = session.get(Silence, identifier)
            row.ends_at = row.ends_at.replace(microsecond=654321)
            session.add(row)
            session.commit()
            rec.sync_keep_silences_to_am(session, [])
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            self.assertEqual(session.get(Silence, identifier).external_context[rec.source_id]["state"], "synced")
        self.assertEqual(self.http.post.call_count, 1)

    def test_lost_update_recovers_millisecond_snapshot_without_duplicate_post(self):
        rec = self.reconciler()
        def accepted_then_lost(*args, **kwargs):
            self.post(*args, **kwargs)
            self.round_remote_timestamps()
            raise requests.ReadTimeout("accepted but acknowledgement lost")
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            external_id = self.remote[0]["id"]
            SilencesBL(session, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"ends_at": "2026-10-05T15:00:00.654321Z"}, correlation_id=None))
            self.http.post.side_effect = accepted_then_lost
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            context = session.get(Silence, identifier).external_context[rec.source_id]
            self.assertEqual(context["state"], "synced")
            self.assertEqual(context["synced_revision"], 2)
        self.assertEqual(self.http.post.call_count, 2)
        self.assertEqual(self.remote[0]["id"], external_id)

    def test_recovery_rejects_deadline_changed_by_one_millisecond(self):
        rec = self.reconciler()
        def accepted_then_changed(*args, **kwargs):
            self.post(*args, **kwargs)
            self.round_remote_timestamps()
            self.remote[-1]["endsAt"] = utc_string(am_time(self.remote[-1]["endsAt"]) + timedelta(milliseconds=1))
            raise requests.ReadTimeout("accepted but acknowledgement lost")
        self.http.post.side_effect = accepted_then_changed
        with Session(self.engine) as session:
            identifier = self.native(session)
            row = session.get(Silence, identifier)
            row.ends_at = row.ends_at.replace(microsecond=654321)
            session.add(row)
            session.commit()
            rec.sync_keep_silences_to_am(session, [])
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            self.assertEqual(session.get(Silence, identifier).external_context[rec.source_id]["state"], "uncertain")
        self.assertEqual(self.http.post.call_count, 1)

    def test_revision_changed_during_http_is_not_acknowledged_as_synced(self):
        def concurrent_update(*args, **kwargs):
            with Session(self.engine) as other:
                SilencesBL(other, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                    schema_version=1, client_request_id=uuid4(), expected_revision=1,
                    changes={"ends_at": "2026-10-05T15:00:00Z"}, correlation_id=None))
            return self.post(*args, **kwargs)
        self.http.post.side_effect = concurrent_update
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            row = session.get(Silence, identifier)
            self.assertEqual(row.revision, 2)
            self.assertEqual(row.external_context[rec.source_id]["synced_revision"], 1)
            self.http.post.side_effect = self.post
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
        self.assertEqual(self.remote[0]["endsAt"], "2026-10-05T15:00:00Z")

    def test_selector_change_retires_old_coverage_before_exporting_new(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            old_id = self.remote[0]["id"]
            SilencesBL(session, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"selector": {"kind": "filter", "cel": 'labels["alertname"] == "Other"'}}, correlation_id=None))
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
        self.http.delete.assert_called_once_with("http://fake-am/api/v2/silence/" + old_id, timeout=10)
        self.assertEqual(len([row for row in self.remote if row["status"]["state"] == "active"]), 1)

    def test_failed_cleanup_is_visible_and_does_not_add_more_coverage(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            SilencesBL(session, self.actor(), now=self.clock).update(identifier, UpdateSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"selector": {"kind": "filter", "cel": 'labels["alertname"] == "Other"'}}, correlation_id=None))
            self.http.delete.side_effect = None
            self.http.delete.return_value = MagicMock(status_code=500)
            rec.sync_keep_silences_to_am(session, copy.deepcopy(self.remote))
            self.assertEqual(SilencesBL(session, self.actor(), now=self.clock).get(identifier).synchronization[0]["reason"], "cancellation_unavailable")
        self.assertEqual(self.http.post.call_count, 1)

    def test_external_cancel_publishes_one_canonical_event(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            self.clock += timedelta(seconds=60)
            rec.sync_keep_silences_to_am(session, [])
            rec.sync_keep_silences_to_am(session, [])
            events = session.exec(select(SilenceEvent).where(SilenceEvent.silence_id == identifier)).all()
            self.assertEqual([row.event_type for row in events], ["silence.created", "silence.cancelled"])
            self.assertEqual(events[-1].payload["actor"]["kind"], "service")

    def test_imported_updates_are_idempotent_and_emit_updated_event(self):
        rec, remote = self.reconciler(), self.am()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [remote])
            remote["comment"] = "changed outside Keep"
            rec.reconcile_silences(session, [remote])
            rec.reconcile_silences(session, [remote])
            events = session.exec(select(SilenceEvent).where(SilenceEvent.team_id == "alpha")).all()
            self.assertEqual([row.event_type for row in events], ["silence.created", "silence.updated"])
            self.assertEqual(events[-1].revision, 2)

    def test_imported_lifecycle_is_atomically_queued_for_configured_subscriber(self):
        self.bundle["subscribers"] = [{"id": "alpha-silences", "team_ids": ["alpha"],
            "event_types": ["silence.created", "silence.updated", "silence.cancelled"], "destination_refs": ["alpha-http"]}]
        self.apply()
        rec, remote = self.reconciler(), self.am()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [remote])
            remote["comment"] = "source update"
            rec.reconcile_silences(session, [remote])
            rec.reconcile_silences(session, [])
            deliveries = session.exec(select(NotificationDelivery)).all()
            self.assertEqual([row.payload["event_type"] for row in deliveries],
                             ["silence.created", "silence.updated", "silence.cancelled"])
            self.assertTrue(all(row.team_id == "alpha" and row.destination_id == "alpha-http" for row in deliveries))

    def test_invalid_snapshot_does_not_cancel_or_partially_import(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [self.am()])
            before = session.exec(select(Silence)).all()
            with self.assertRaises(ValueError):
                rec.reconcile_silences(session, [self.am(), self.am(matchers=[])])
            self.assertEqual(len(session.exec(select(Silence)).all()), len(before))
            self.assertTrue(all(row.cancelled_at is None for row in before))

    def test_switching_source_endpoint_does_not_cancel_or_update_other_receipts(self):
        remote = self.am()
        first, second = self.reconciler(), self.reconciler()
        second.alertmanager_url = "http://second-am"
        second.source_id = "second-source"
        with Session(self.engine) as session:
            first.reconcile_silences(session, [remote])
            second.reconcile_silences(session, [remote])
            second.reconcile_silences(session, [])
            original = [row for row in session.exec(select(Silence)).all() if first.source_id in row.external_context]
            self.assertEqual(len(original), 3)
            self.assertTrue(all(row.cancelled_at is None for row in original))

    def test_healthy_empty_inventory_recovers_after_circuit_grace(self):
        self.save(self.event("ghost"))
        rec = self.reconciler(circuit_grace_seconds=30)
        with Session(self.engine) as session, patch("keep.api.core.alertmanager_reconciliation.process_event"):
            self.assertTrue(rec.reconcile_alerts(session, [])["circuit_broken"])
            self.clock += timedelta(seconds=31)
            self.assertEqual(rec.reconcile_alerts(session, [])["misses_tracked"], {"ghost": 1})
            self.clock += timedelta(seconds=60)
            self.assertEqual(rec.reconcile_alerts(session, [])["resolved"], ["ghost"])

    def test_worker_cooldown_is_shared_across_instances(self):
        self.http.get.return_value = MagicMock(status_code=200)
        self.http.get.return_value.json.return_value = []
        with Session(self.engine) as session:
            self.reconciler().reconcile_once(session)
            result = self.reconciler().reconcile_once(session)
        self.assertEqual(result, {"skipped": "poll_interval"})
        self.assertEqual(self.http.get.call_count, 2)

    def test_failing_poll_breaks_consecutive_misses_and_circuit_confirmation(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            rec.reconcile_once(session)
            state = session.get(AlertmanagerReconciliationState, ("tenant", rec.source_id))
            state.next_run_at = None
            state.alert_state = {"misses": {"ghost": 1}, "breaker_since": self.clock.isoformat(), "breaker_fingerprints": []}
            session.add(state)
            session.commit()
            self.http.get.side_effect = requests.ConnectionError("unreachable endpoint")
            result = rec.reconcile_once(session)
            self.assertEqual(result["error"], "alertmanager_unavailable")
            session.refresh(state)
            self.assertEqual(state.alert_state["misses"], {})
            self.assertIsNone(state.alert_state["breaker_since"])

    def test_private_receipts_cannot_be_submitted_or_returned_through_api(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session)
            rec.sync_keep_silences_to_am(session, [])
            public = SilencesBL(session, self.actor(), now=self.clock).get(identifier).json()
            self.assertNotIn("external_context", public)
            self.assertNotIn("keep-sync:", public)
            self.assertNotIn(self.remote[0]["id"], public)
        with self.assertRaises(ValueError):
            UpdateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": 1,
                "changes": {"external_context": {"external_id": "foreign"}}, "correlation_id": None})

    def test_legacy_public_binding_is_never_adopted_as_external_authority(self):
        with Session(self.engine) as session:
            identifier = self.native(session, correlation_id="am_synced:legacy")
            self.reconciler().sync_keep_silences_to_am(session, [self.am(id="legacy", createdBy="keep")])
            self.assertEqual(SilencesBL(session, self.actor(), now=self.clock).get(identifier).synchronization[0]["reason"],
                             "legacy_binding_requires_review")
        self.http.post.assert_not_called()
        self.http.delete.assert_not_called()

    def test_literal_conjunctions_export_but_boolean_approximations_never_do(self):
        from keep.api.core.alertmanager_matchers import filter_matchers, UnsupportedSelector
        matchers = filter_matchers('labels.alertname == "HighCpu" && labels["namespace"] == "a"')
        self.assertEqual({row["name"] for row in matchers}, {"alertname", "namespace"})
        for expression in ('labels.alertname == "A" || labels.alertname == "B"',
                           '!(labels.alertname == "A")', 'labels.empty == ""',
                           'labels.alertname == "A" && severity > 1', 'labels.alertname.matches("A.*")'):
            with self.subTest(expression=expression), self.assertRaises(UnsupportedSelector):
                filter_matchers(expression)

    def test_incoming_regex_full_match_including_newline_and_flags(self):
        env = celpy.Environment()
        for pattern, value, expected in (("auth", "auth\n", False), ("(?i)auth", "AUTH", True),
                                         (".*", "", True), ("(?s).*", "auth\n", True),
                                         (r"\w+", "сервис", False), (r"\p{L}+", "сервис", True),
                                         ("(?U).*", "auth", True)):
            expression = matchers_to_cel([{"name": "service", "value": pattern, "isRegex": True, "isEqual": True}])
            self.assertEqual(bool(compile_filter(expression, alertmanager=True).evaluate(celpy.json_to_cel({"labels": {"service": value}}))), expected)

    def test_explicit_disable_and_iac_scope_configuration(self):
        from keep.api.tasks import process_alertmanager_reconciliation_task as worker
        with patch.object(worker, "_RECONCILER", None), patch.dict(os.environ, {
            "KEEP_ALERTMANAGER_URL": "http://am", "KEEP_ALERTMANAGER_RECONCILER_ENABLED": "false"}):
            self.assertIsNone(worker.get_reconciler())
        with patch.object(worker, "_RECONCILER", None), patch.dict(os.environ, {
            "KEEP_ALERTMANAGER_URL": "http://am", "KEEP_ALERTMANAGER_RECONCILER_ENABLED": "true",
            "KEEP_ALERTMANAGER_TEAM_MATCHERS": json.dumps({"arbitrary-team": [[{"name": "owner", "value": "engineers"}]]})}):
            self.assertEqual(worker.get_reconciler().team_matchers["arbitrary-team"][0][0]["value"], "engineers")

    def test_overlapping_iac_team_boundaries_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "disjoint"):
            self.reconciler(team_matchers={"first": [[{"name": "env", "value": "shared"}]],
                                          "second": [[{"name": "env", "value": "shared"}]]})

    def test_historical_ownership_change_cannot_export_foreign_fingerprint(self):
        self.save(self.event("shared", alertname="HighCpu", zone="ALPHA"))
        rec = self.reconciler()
        with Session(self.engine) as session:
            identifier = self.native(session, selector={"kind": "alert", "fingerprints": ["shared"]})
            session.add(Alert(tenant_id="tenant", team_id="beta", fingerprint="shared", provider_type="keep",
                              timestamp=self.clock, event={"labels": {"alertname": "HighCpu", "zone": "BETA"}}))
            session.commit()
            rec.sync_keep_silences_to_am(session, [])
            self.assertEqual(SilencesBL(session, self.actor(role="admin"), now=self.clock).get(identifier).synchronization[0]["reason"],
                             "foreign_alert_history")
        self.http.post.assert_not_called()

    def test_legacy_imported_actor_is_readable_before_first_reconciliation(self):
        rec = self.reconciler()
        with Session(self.engine) as session:
            rec.reconcile_silences(session, [self.am()])
            row = session.exec(select(Silence).where(Silence.team_id == "alpha")).one()
            row.created_by = row.updated_by = {"kind": "system", "name": "old-operator"}
            session.add(row)
            session.commit()
            resource = SilencesBL(session, self.actor(), now=self.clock).get(row.id)
            self.assertEqual(resource.created_by.kind, "service")
            self.assertEqual(resource.created_by.display_name, "old-operator")
            self.assertTrue(resource.read_only)
