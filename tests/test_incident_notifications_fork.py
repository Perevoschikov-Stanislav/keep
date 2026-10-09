"""Task 29: canonical routing and the shared durable delivery queue."""

import copy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlmodel import Session, select

from keep.api.models.db.silence import NotificationDelivery
from tests.test_incident_automation_fork import AutomationCase


EVENTS = ["incident." + name for name in (
    "created", "updated", "acknowledged", "resolved", "reopened", "escalated", "reminder")]


class NotificationCase(AutomationCase):
    def setUp(self):
        import keep.api.core.incident_notifications  # noqa: F401
        super().setUp()
        self.enterContext(patch.dict("os.environ", {"KEEP_NOTIFICATION_TEST_KEY": "local-fixture-only"}))
        self.bundle["transports"] = [
            {"id": "webhook", "kind": "http_json", "adapter_ref": "http-json-v1",
             "endpoint": "https://receiver.example.org", "auth_ref": None,
             "capabilities": {"update": False, "actions": False, "receipts": False}},
            {"id": "chat-api", "kind": "mattermost", "adapter_ref": "mattermost-api-v1",
             "endpoint": "https://chat.example.org", "auth_ref": "env:KEEP_NOTIFICATION_TEST_KEY",
             "capabilities": {"update": True, "actions": False, "receipts": True}},
        ]
        for transport in self.bundle["transports"]:
            transport["delivery"] = {"timeout_seconds": 1, "retry": {"max_attempts": 3,
                "initial_backoff_seconds": 1, "max_backoff_seconds": 3, "multiplier": 2},
                "rate_limit": {"per_second": 100, "burst": 100}, "debounce_seconds": 0}
        self.bundle["destinations"] = [
            {"id": team + "-" + kind, "team_id": team, "transport_ref": "webhook" if kind == "http" else "chat-api",
             "options": {"path": "/events"} if kind == "http" else {"channel_id": team + "-channel"}}
            for team in ("alpha", "beta") for kind in ("http", "chat")]
        self.bundle["presentations"][0]["actions"] = [{"command": "ack", "label": "Acknowledge"}]
        self.bundle["routes"] = [{"id": "engineering", "team_ids": ["alpha", "beta"], "priority": 100,
            "match": "true", "event_types": EVENTS,
            "destination_refs": [item["id"] for item in self.bundle["destinations"]],
            "presentation_ref": "workload", "delivery_mode": "upsert",
            "update_fallback": "append", "actions_fallback": "keep_link"}]
        self.apply()
        self.sent = []

    def dispatcher(self, seconds=0, sender=None):
        from keep.api.core.incident_notifications import IncidentNotificationWorker
        from keep.api.core.silence_integrations import SilenceIntegrations
        with Session(self.engine) as session:
            from keep.api.models.db.incident_configuration import IncidentConfiguration
            settings = SilenceIntegrations.from_snapshot(session.get(IncidentConfiguration, "tenant").snapshot)
        return IncidentNotificationWorker(self.engine, settings, clock=lambda: self.now(seconds), sender=sender or self.send)

    def send(self, delivery, transport):
        self.sent.append(copy.deepcopy(delivery.payload))
        if delivery.context.get("kind") != "incident":
            return 200
        return {"status": "delivered", "external_id": delivery.id.hex[:26] if transport["capabilities"]["receipts"] else None}

    def actor(self, role="responder", team="alpha"):
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        from keep.api.models.silence import SilenceActor
        return AuthenticatedEntity(tenant_id="tenant", email="operator", role=role,
            teams=frozenset({team}), visible_teams=frozenset({team}),
            verified_silence_actor=SilenceActor(kind="user", subject="operator", issuer="https://identity.example.org", display_name="operator"))

    def command(self, name="ack", **fields):
        from keep.api.models.incident_notification import IncidentCommand
        row = self.incidents()[0]
        return IncidentCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
            "incident_id": str(row.id), "expected_revision": (row.lifecycle_context or {}).get("revision", 0),
            "command": name, "correlation_id": None, **fields})

    def deliveries(self):
        with Session(self.engine) as session:
            return session.exec(select(NotificationDelivery).order_by(NotificationDelivery.created_at, NotificationDelivery.id)).all()

    def resume_after_silence(self):
        self.bundle["routes"][0]["after_silence"] = "current_active"
        self.apply()

    def silence_rule(self, *, start=None, end=20, selector=None):
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CreateSilenceCommand, utc_string
        from keep.api.models.db.user import User
        actor = self.actor()
        with Session(self.engine) as session:
            if not session.exec(select(User).where(User.tenant_id == "tenant", User.username == actor.email)).first():
                session.add(User(tenant_id="tenant", username=actor.email, role="responder", password_hash="unused"))
                session.commit()
            command = CreateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
                "team_id": "alpha", "selector": selector or {"kind": "incident", "incident_ids": [str(self.incidents()[0].id)]},
                "starts_at": utc_string(self.now(start)) if start is not None else None,
                "ends_at": utc_string(self.now(end)), "comment": "Notification resume verification", "correlation_id": None})
            result, _ = SilencesBL(session, actor, self.origin).create(command)
            return result.result

    def cancel_rule(self, rule, seconds):
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CancelSilenceCommand
        with Session(self.engine) as session:
            return SilencesBL(session, self.actor(), self.now(seconds)).cancel(rule.id,
                CancelSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=rule.revision,
                    reason="Resume verification", correlation_id=None))


class IncidentNotificationsTest(NotificationCase):
    def test_routing_creation_fence_preserves_history_without_resending_it(self):
        from keep.api.core.incident_notifications import canonical
        cutoff = self.now(1).isoformat() + "Z"
        self.bundle["routes"][0]["match"] = f"incident.created_at >= '{cutoff}'"
        self.apply()
        self.correlate(self.event("historical"))
        old = self.incidents()[0]
        from keep.api.models.db.incident import Incident
        with Session(self.engine) as session:
            row = session.get(Incident, old.id)
            row.creation_time = self.origin
            session.add(row)
            session.commit()
        old.creation_time = self.origin
        self.dispatcher(2).run_once()
        self.assertEqual(self.sent, [])
        self.correlate(self.event("new", workload="new"), 2)
        with Session(self.engine) as session:
            row = next(row for row in session.exec(select(Incident)).all() if row.id != old.id)
            row.creation_time = self.now(2)
            session.add(row)
            session.commit()
        self.dispatcher(3).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(canonical(old, self.now(3))["created_at"], self.origin.isoformat() + "Z")

    def test_invalid_routing_or_capabilities_never_replace_active_configuration(self):
        from keep.api.models.db.incident_configuration import IncidentConfiguration
        with Session(self.engine) as session:
            before = session.get(IncidentConfiguration, "tenant").digest
        original = copy.deepcopy(self.bundle)
        for edit in (lambda b: b["routes"][0].update(match="invalid ("),
                     lambda b: b["transports"][1]["capabilities"].update(actions=True),
                     lambda b: b["routes"][0].update(update_fallback="reject")):
            self.bundle = copy.deepcopy(original)
            edit(self.bundle)
            with self.assertRaises(ValueError):
                self.apply()
            with Session(self.engine) as session:
                self.assertEqual(session.get(IncidentConfiguration, "tenant").digest, before)

    def test_fanout_uses_one_canonical_incident_and_two_adapter_profiles(self):
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({item["destination_ref"] for item in self.sent}, {"alpha-http", "alpha-chat"})
        self.assertEqual({item["incident_id"] for item in self.sent}, {str(self.incidents()[0].id)})
        self.assertEqual({item["title"] for item in self.sent}, {"catalog"})
        self.assertTrue(all(item["event_type"] == "incident.created" for item in self.sent))
        self.assertTrue(all(row.state == "delivered" for row in self.deliveries()))

    def test_repeated_scan_and_apply_do_not_create_more_deliveries(self):
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.apply()
        self.dispatcher(1).run_once()
        self.assertEqual(len(self.deliveries()), 2)
        self.assertEqual(len(self.sent), 2)

    def test_silenced_notification_is_never_replayed_after_expiry(self):
        self.correlate(self.event())
        self.silence(end=20)
        self.dispatcher(1).run_once()
        self.assertEqual(self.sent, [])
        self.assertTrue(all(row.state == "skipped" for row in self.deliveries()))
        self.dispatcher(21).run_once()
        self.assertEqual(self.sent, [])

    def test_expiry_sends_one_current_projection_without_replaying_skipped_deliveries(self):
        self.resume_after_silence()
        self.correlate(self.event())
        before = self.incidents()[0]
        self.silence_rule()
        self.dispatcher(1).run_once()
        suppressed = {row.id for row in self.deliveries()}
        self.assertEqual(self.sent, [])
        self.dispatcher(21).run_once()
        self.dispatcher(22).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({row["event_type"] for row in self.sent}, {"incident.updated"})
        self.assertEqual({row["projection_revision"] for row in self.sent}, {2})
        self.assertTrue(all(row.state == "skipped" for row in self.deliveries() if row.id in suppressed))
        after = self.incidents()[0]
        self.assertEqual(after.lifecycle_context, before.lifecycle_context)
        self.assertEqual(after.automation_context, before.automation_context)
        self.assertEqual(after.notification_context["last_decision"]["trigger"], "silence_ended")

    def test_resume_policy_rejects_a_route_that_cannot_receive_current_updates(self):
        self.bundle["routes"][0]["after_silence"] = "current_active"
        self.bundle["routes"][0]["event_types"] = ["incident.created"]
        with self.assertRaisesRegex(ValueError, "requires incident.updated"):
            self.apply()

    def test_active_only_initial_delivery_skips_closed_history_and_sends_later_resolution(self):
        self.bundle["routes"][0]["initial_delivery"] = "active_only"
        self.apply()
        self.correlate(self.event())
        self.change("resolved", 1)
        self.dispatcher(2).run_once()
        self.assertEqual(self.sent, [])
        self.assertTrue(all(row.last_error_code == "initial_target_inactive" for row in self.deliveries()))
        self.correlate(self.event(), 3)
        self.dispatcher(3).run_once()
        self.assertEqual(len(self.sent), 2)
        self.change("resolved", 4)
        self.dispatcher(4).run_once()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual({row["event_type"] for row in self.sent[-2:]}, {"incident.resolved"})

    def test_active_only_initial_delivery_rechecks_resolution_after_enqueue(self):
        self.bundle["routes"][0]["initial_delivery"] = "active_only"
        self.apply()
        self.correlate(self.event())
        worker = self.dispatcher(0)
        worker._scan()
        claimed = worker.claim()
        self.change("resolved", 1)
        self.assertFalse(self.dispatcher(2).send_claimed(claimed))
        self.dispatcher(3).run_once()
        self.assertEqual(self.sent, [])

    def test_presentation_config_refreshes_saved_sources_without_new_ingestion(self):
        event = self.event()
        event.description = "Stored engineer detail"
        event.generatorURL = "https://prom.example.org/graph?expr=up"
        self.correlate(event)
        self.dispatcher(0).run_once()
        before = self.incidents()[0]
        definition = self.bundle["presentations"][0]
        definition["description"] = "{{ incident.collections.details }}"
        definition["collections"] = [{"id": "details", "sources": ["description"]}]
        definition["source_links"] = [{"label": "Prometheus", "sources": ["generatorURL"]}]
        self.apply()
        self.dispatcher(1).run_once()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual({row["description"] for row in self.sent[-2:]}, {"Stored engineer detail"})
        self.assertTrue(all(row["links"] == [{"label": "Prometheus", "url": event.generatorURL}] for row in self.sent[-2:]))
        after = self.incidents()[0]
        self.assertEqual(after.lifecycle_context, before.lifecycle_context)
        self.assertEqual(after.normalization_context["normalized"], before.normalization_context["normalized"])
        self.dispatcher(2).run_once()
        self.assertEqual(len(self.sent), 4)

    def test_selected_historical_active_incident_gets_new_bindings_without_legacy_sqlite(self):
        from keep.api.models.db.incident import Incident
        self.bundle["routes"][0]["initial_delivery"] = "active_only"
        cutoff = self.now(1).isoformat() + "Z"
        self.bundle["routes"][0]["match"] = f"incident.created_at >= '{cutoff}'"
        self.apply()
        self.correlate(self.event("active"))
        self.correlate(self.event("closed", workload="other"))
        rows = self.incidents()
        with Session(self.engine) as session:
            for item in rows:
                row = session.get(Incident, item.id)
                row.creation_time = self.origin - timedelta(days=1)
                row.user_generated_name, row.user_summary, row.assignee = "Existing title", "Existing notes", "operator"
                row.status = "firing" if row.normalization_context["normalized"]["workload"] == "catalog" else "resolved"
                if row.status == "firing":
                    active_id = row.id
                session.add(row)
            session.commit()
        self.dispatcher(2).run_once()
        self.assertEqual(self.sent, [])
        selected = "['" + "','".join(str(row.id) for row in rows) + "']"
        self.bundle["routes"][0]["match"] += " || incident.id in " + selected
        self.apply()
        self.dispatcher(3).run_once()
        self.dispatcher(4).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({row["incident_id"] for row in self.sent}, {str(active_id)})
        self.assertEqual({row["title"] for row in self.sent}, {"Existing title"})
        self.assertEqual({row["description"] for row in self.sent}, {"Existing notes"})
        self.assertTrue(any(row.last_error_code == "initial_target_inactive" for row in self.deliveries()))

    def test_repeated_active_alerts_do_not_move_episode_times_or_starve_debounce(self):
        from keep.api.core.incident_notifications import canonical
        for transport in self.bundle["transports"]:
            transport["delivery"]["debounce_seconds"] = 60
        self.apply()
        self.correlate(self.event())
        self.dispatcher(0).run_once()
        first = canonical(self.incidents()[0], self.now(0))
        for seconds in (20, 40):
            self.correlate(self.event(), seconds)
            self.dispatcher(seconds).run_once()
            current = canonical(self.incidents()[0], self.now(seconds))
            self.assertEqual(current["start_time"], first["start_time"])
            self.assertIsNone(current["end_time"])
        self.dispatcher(60).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.deliveries()), 2)
        self.assertEqual(self.incidents()[0].notification_context["sequence"], 1)

    def test_cancel_waits_for_all_overlapping_rules_and_preserves_ack(self):
        self.resume_after_silence()
        self.correlate(self.event())
        self.change("acknowledged", 0)
        first, second = self.silence_rule(), self.silence_rule(end=30)
        self.dispatcher(1).run_once()
        self.cancel_rule(first, 2)
        self.dispatcher(2).run_once()
        self.assertEqual(self.sent, [])
        self.cancel_rule(second, 3)
        self.dispatcher(3).run_once()
        self.dispatcher(4).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.incidents()[0].status, "acknowledged")
        self.assertEqual({row["projection_revision"] for row in self.sent}, {2})

    def test_scheduled_silence_tracks_suppression_without_changing_the_incident(self):
        self.resume_after_silence()
        self.correlate(self.event())
        self.dispatcher(0).run_once()
        self.silence_rule(start=5, end=10)
        self.dispatcher(4).run_once()
        self.assertFalse(self.incidents()[0].notification_context["silence_blocked"])
        self.dispatcher(6).run_once()
        self.assertTrue(self.incidents()[0].notification_context["silence_blocked"])
        self.assertEqual(len(self.sent), 2)
        self.dispatcher(11).run_once()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.incidents()[0].notification_context["sequence"], 2)

    def test_resolved_incident_is_not_sent_when_silence_ends(self):
        self.resume_after_silence()
        self.correlate(self.event())
        self.silence_rule()
        self.dispatcher(1).run_once()
        self.change("resolved", 5)
        self.dispatcher(5).run_once()
        self.dispatcher(21).run_once()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.incidents()[0].notification_context["silence_blocked"])

    def test_partial_coverage_resumes_only_after_it_ends(self):
        self.resume_after_silence()
        self.correlate(self.event("pod-a"))
        self.correlate(self.event("pod-b"), 1)
        self.silence_rule(selector={"kind": "alert", "fingerprints": ["pod-a"]})
        self.dispatcher(2).run_once()
        self.assertEqual(self.sent, [])
        self.assertTrue(all(row.last_error_code == "silence_partial_payload_unsafe" for row in self.deliveries()))
        self.dispatcher(21).run_once()
        self.assertEqual(len(self.sent), 2)

    def test_silence_created_after_claim_still_allows_one_fresh_projection_at_expiry(self):
        self.resume_after_silence()
        self.correlate(self.event())
        worker = self.dispatcher(0)
        worker._scan()
        claimed = worker.claim()
        self.silence_rule()
        self.assertFalse(self.dispatcher(1).send_claimed(claimed))
        self.dispatcher(21).run_once()
        self.dispatcher(22).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({row["projection_revision"] for row in self.sent}, {2})

    def test_destination_operations_enter_the_shared_queue(self):
        self.bundle["automation"][0]["levels"][0].update(workflow_refs=[], destination_refs=["alpha-http", "beta-http"])
        self.apply()
        self.correlate(self.event())
        self.dispatcher(0).run_once()
        self.sent.clear()
        self.tick(10)
        self.dispatcher(10).run_once()
        self.assertEqual([item["event_type"] for item in self.sent], ["incident.escalated"])
        self.assertEqual(self.operations()[0].status, "success")

    def test_actor_commands_have_revision_checks_and_idempotency(self):
        from keep.api.core.incident_notifications import execute_incident_command
        from keep.api.models.incident_notification import IncidentCommand
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        from keep.api.models.silence import SilenceActor
        self.correlate(self.event())
        incident = self.incidents()[0]
        actor = AuthenticatedEntity(tenant_id="tenant", email="operator", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}),
            verified_silence_actor=SilenceActor(kind="user", subject="operator", issuer="https://identity.example.org", display_name="operator"))
        command = IncidentCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
            "incident_id": str(incident.id), "expected_revision": incident.lifecycle_context["revision"],
            "command": "ack", "correlation_id": None})
        with Session(self.engine) as session:
            result = execute_incident_command(session, actor, command, now=self.now(1))
        self.assertEqual(result["status"], "acknowledged")
        with Session(self.engine) as session:
            replay = execute_incident_command(session, actor, command, now=self.now(2))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], result["revision"])

    def test_initial_revision_cannot_be_reused_after_ack(self):
        from fastapi import HTTPException
        from keep.api.core.incident_notifications import execute_incident_command
        self.correlate(self.event())
        command = self.command()
        with Session(self.engine) as session:
            result = execute_incident_command(session, self.actor(), command, now=self.now(1))
        self.assertEqual(result["revision"], command.expected_revision + 1)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
            execute_incident_command(session, self.actor(), self.command("resolve", expected_revision=command.expected_revision), now=self.now(2))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.incidents()[0].status, "acknowledged")

    def test_claimed_delivery_checks_a_new_silence(self):
        self.correlate(self.event())
        worker = self.dispatcher()
        worker._scan()
        row = worker.claim()
        self.silence()
        self.assertFalse(self.dispatcher(1).send_claimed(row))
        self.assertEqual(self.sent, [])
        self.assertEqual(next(item for item in self.deliveries() if item.id == row.id).state, "skipped")

    def test_mixed_alert_history_does_not_leak_through_notification(self):
        self.correlate(self.event())
        self.save(self.event(team="beta", namespace="foreign"), 1)
        self.dispatcher(1).run_once()
        self.assertEqual(self.sent, [])
        self.assertTrue(all(row.last_error_code == "foreign_alert_history" for row in self.deliveries()))

    def test_higher_priority_route_and_scope_control_fanout(self):
        high = {**self.bundle["routes"][0], "id": "critical", "priority": 200,
                "team_ids": ["beta"], "destination_refs": ["beta-http"], "match": "incident.severity == 'critical'"}
        self.bundle["routes"].append(high)
        self.apply()
        self.correlate(self.event(team="beta", workload="critical"))
        with Session(self.engine) as session:
            from keep.api.models.db.incident import Incident, IncidentSeverity
            row = session.get(Incident, self.incidents()[0].id)
            row.severity = IncidentSeverity.CRITICAL.order
            session.add(row)
            session.commit()
        self.dispatcher().run_once()
        self.assertEqual([item["destination_ref"] for item in self.sent], ["beta-http"])

    def test_no_matching_route_never_uses_another_team(self):
        self.bundle["routes"][0]["team_ids"] = ["beta"]
        self.bundle["routes"][0]["destination_refs"] = ["beta-http", "beta-chat"]
        self.apply()
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.incidents()[0].notification_context["last_decision"]["reason"], "no_route")

    def test_debounce_sends_only_the_latest_committed_projection(self):
        for transport in self.bundle["transports"]:
            transport["delivery"]["debounce_seconds"] = 5
        self.apply()
        self.correlate(self.event())
        self.dispatcher(0).run_once()
        self.change("acknowledged", 1)
        self.dispatcher(1).run_once()
        self.dispatcher(5).run_once()
        self.assertEqual(self.sent, [])
        self.dispatcher(6).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(item["event_type"] == "incident.acknowledged" for item in self.sent))
        self.assertTrue(all(item["incident_revision"] == 1 for item in self.sent))

    def test_unknown_create_holds_only_its_destination(self):
        def uncertain(row, transport):
            if row.destination_id == "alpha-chat":
                return {"status": "unknown", "code": "transport_result_unknown"}
            return self.send(row, transport)
        self.correlate(self.event())
        self.dispatcher(0, uncertain).run_once()
        self.change("acknowledged", 1)
        self.dispatcher(1).run_once()
        self.assertEqual([item["destination_ref"] for item in self.sent], ["alpha-http", "alpha-http"])
        self.assertEqual(sum(row.state == "unknown" for row in self.deliveries()), 1)
        self.assertEqual(sum(row.state == "pending" for row in self.deliveries()), 1)

    def test_safe_connect_failure_uses_configured_capped_retries(self):
        def failed(row, transport):
            if row.destination_id == "alpha-chat":
                return {"status": "failed", "code": "connect_timeout"}
            return self.send(row, transport)
        self.correlate(self.event())
        for seconds in (0, 1, 2, 3, 7):
            self.dispatcher(seconds, failed).run_once()
        row = next(item for item in self.deliveries() if item.destination_id == "alpha-chat")
        self.assertEqual((row.state, row.attempts), ("failed", 3))
        self.assertEqual(len(self.sent), 1)

    def test_expired_unstarted_claim_is_fenced(self):
        self.correlate(self.event())
        worker = self.dispatcher()
        worker._scan()
        old = worker.claim()
        later = worker.settings.dispatch["lease_seconds"] + 1
        current = self.dispatcher(later).claim()
        self.assertEqual(current.id, old.id)
        self.assertNotEqual(current.lease_token, old.lease_token)
        self.assertFalse(self.dispatcher(later).send_claimed(old))
        self.assertEqual(self.sent, [])

    def test_crash_after_effect_start_is_not_retried_blindly(self):
        def stopped(row, transport):
            raise SystemExit("Simulated worker termination")
        self.correlate(self.event())
        worker = self.dispatcher(sender=stopped)
        worker._scan()
        old = worker.claim()
        with self.assertRaises(SystemExit):
            worker.send_claimed(old)
        self.dispatcher(worker.settings.dispatch["lease_seconds"] + 1).run_once()
        recovered = next(item for item in self.deliveries() if item.id == old.id)
        self.assertEqual((recovered.state, recovered.attempts), ("unknown", 1))
        self.assertNotIn(str(old.id), [item["notification_id"] for item in self.sent])

    def test_ack_cancels_a_destination_operation_already_in_the_queue(self):
        self.bundle["automation"][0]["levels"][0].update(workflow_refs=[], destination_refs=["alpha-http", "beta-http"])
        self.apply()
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.sent.clear()
        self.tick(10)
        self.dispatcher(10)._scan()
        self.change("acknowledged", 11)
        self.dispatcher(11).run_once()
        self.assertFalse(any(item["event_type"] == "incident.escalated" for item in self.sent))
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_configuration_change_retires_the_claimed_destination(self):
        self.correlate(self.event())
        worker = self.dispatcher()
        worker._scan()
        old = worker.claim()
        self.bundle["transports"][0]["endpoint"] = "https://another.example.org"
        self.bundle["transports"][1]["endpoint"] = "https://other-chat.example.org"
        self.apply()
        self.assertFalse(self.dispatcher(1).send_claimed(old))
        self.assertEqual(self.sent, [])

    def test_viewer_and_foreign_actor_cannot_mutate_incident(self):
        from fastapi import HTTPException
        from keep.api.core.incident_notifications import execute_incident_command
        self.correlate(self.event())
        for role, team, expected in (("viewer", "alpha", 403), ("responder", "beta", 404)):
            with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
                execute_incident_command(session, self.actor(role, team), self.command(), now=self.now(1))
            self.assertEqual(error.exception.status_code, expected)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_assign_is_canonical_and_cannot_invent_an_operator(self):
        from fastapi import HTTPException
        from keep.api.core.incident_notifications import execute_incident_command
        self.correlate(self.event())
        with Session(self.engine) as session:
            result = execute_incident_command(session, self.actor(), self.command("assign", assignee="operator"), now=self.now(1))
        self.assertEqual(result["assignee"], "operator")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
            execute_incident_command(session, self.actor(), self.command("assign", assignee="invented"), now=self.now(2))
        self.assertEqual(error.exception.status_code, 403)

    def test_partial_silence_blocks_the_entire_incident_payload(self):
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CreateSilenceCommand, utc_string
        from keep.api.models.db.user import User
        self.correlate(self.event("pod-a"))
        self.correlate(self.event("pod-b"), 1)
        actor = self.actor()
        with Session(self.engine) as session:
            session.add(User(tenant_id="tenant", username=actor.email, role="responder", password_hash="unused"))
            session.commit()
            command = CreateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
                "team_id": "alpha", "selector": {"kind": "alert", "fingerprints": ["pod-a"]},
                "starts_at": None, "ends_at": utc_string(self.now(20)), "comment": "Partial coverage", "correlation_id": None})
            SilencesBL(session, actor, self.now(2)).create(command)
        self.dispatcher(3).run_once()
        self.assertEqual(self.sent, [])
        self.assertTrue(all(row.last_error_code == "silence_partial_payload_unsafe" for row in self.deliveries()))

    def test_delayed_second_sender_cannot_take_over_an_expired_effect(self):
        self.bundle["automation"][0]["levels"][0].update(workflow_refs=[], destination_refs=["alpha-chat", "beta-chat"])
        self.bundle["automation"][0]["levels"][0]["repeat_every_seconds"] = 0
        self.bundle["automation"][0]["levels"][0]["repeat_limit"] = 1
        self.bundle["automation"][0]["levels"] = self.bundle["automation"][0]["levels"][:1]
        self.bundle["automation"][0]["reminder"] = {"every_seconds": 10, "destination_refs": ["alpha-chat", "beta-chat"]}
        self.apply()
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.tick(10)
        first = self.dispatcher(10)
        first._scan()
        with Session(self.engine) as session:
            for delivery in session.exec(select(NotificationDelivery).where(NotificationDelivery.state == "pending")).all():
                if delivery.payload["event_type"] == "incident.reminder":
                    delivery.available_at = self.now(9)
                    session.add(delivery)
            session.commit()
        old = first.claim()
        def stopped(row, transport):
            raise SystemExit("Simulated worker termination")
        first.sender = stopped
        with self.assertRaises(SystemExit):
            first.send_claimed(old)
        waiting = self.dispatcher(11).claim()
        self.assertEqual(waiting.destination_id, old.destination_id)
        before = len(self.sent)
        self.assertFalse(self.dispatcher(10 + first.settings.dispatch["lease_seconds"] + 0.5).send_claimed(waiting))
        self.assertEqual(len(self.sent), before)
        self.assertEqual(next(row for row in self.deliveries() if row.id == old.id).state, "unknown")

    def test_small_scan_batches_eventually_visit_every_canonical_incident(self):
        self.bundle["dispatch"] = {"batch_size": 1}
        self.apply()
        for number in range(3):
            self.correlate(self.event(str(number), workload="workload-" + str(number)))
        for number in range(9):
            self.dispatcher(number).run_once()
        self.assertEqual(len(self.sent), 6)
        self.assertEqual({item["incident_id"] for item in self.sent}, {str(item.id) for item in self.incidents()})

    def test_silence_service_event_uses_the_same_worker_without_ordinary_gate(self):
        self.bundle["subscribers"] = [{"id": "silence-service", "team_ids": ["alpha"],
            "event_types": ["silence.created"], "destination_refs": ["alpha-http"]}]
        self.apply()
        self.correlate(self.event())
        from keep.api.core.incident_configuration import configuration_scope
        with configuration_scope("tenant"):
            self.silence()
        self.dispatcher(1).run_once()
        self.assertEqual([item["event_type"] for item in self.sent], ["silence.created"])

    def test_contacts_and_manual_values_are_rendered_from_current_iac_and_canonical_state(self):
        from keep.api.models.db.incident import Incident
        self.bundle["contacts"] = [{"id": team + "-oncall", "team_id": team, "label": team,
            "addresses": [{"transport_ref": transport["id"], "address": team + "-operator"} for transport in self.bundle["transports"]]}
            for team in ("alpha", "beta")]
        self.bundle["routes"][0]["contact_refs"] = ["alpha-oncall", "beta-oncall"]
        self.bundle["presentations"][0]["fields"].append({"path": "incident.assignee", "label": "Owner", "order": 2})
        self.apply()
        self.correlate(self.event())
        worker = self.dispatcher()
        worker._scan()
        with Session(self.engine) as session:
            row = session.get(Incident, self.incidents()[0].id)
            row.user_generated_name, row.user_summary, row.assignee = "Manual title", "Manual summary", "operator"
            session.add(row)
            session.commit()
        self.dispatcher(1).run_once()
        self.assertTrue(all(item["title"] == "Manual title" and item["description"] == "Manual summary" for item in self.sent))
        self.assertTrue(all(item["contact_refs"] == ["alpha-oncall"] for item in self.sent))
        self.assertTrue(all({"label": "Owner", "value": "operator"} in item["fields"] for item in self.sent))

    def test_quiet_flap_reset_updates_projection_without_a_new_event(self):
        self.bundle["lifecycle"][0]["flapping"].update(enabled=True, transition_threshold=2)
        self.bundle["presentations"][0]["fields"].append({"path": "incident.flapping.active", "label": "Flapping", "order": 2})
        self.apply()
        self.correlate(self.event())
        self.dispatcher().run_once()
        self.change("resolved", 1)
        self.dispatcher(1).run_once()
        self.change("firing", 2)
        self.dispatcher(2).run_once()
        before = len(self.sent)
        self.dispatcher(61).run_once()
        self.assertEqual(len(self.sent), before)
        self.dispatcher(63).run_once()
        self.assertEqual(len(self.sent), before + 2)
        self.assertTrue(all({"label": "Flapping", "value": "False"} in item["fields"] for item in self.sent[-2:]))

    def test_late_provider_version_does_not_produce_a_stale_notification(self):
        self.bundle["lifecycle"][0]["clock"] = "event_time"
        self.apply()
        first = self.event()
        first.lastReceived = self.now(0).isoformat()
        self.correlate(first)
        self.dispatcher().run_once()
        resolved = self.event(status="resolved")
        resolved.lastReceived = self.now(20).isoformat()
        self.correlate(resolved, 21)
        self.dispatcher(21).run_once()
        before = len(self.sent)
        late = self.event(workload="old-workload")
        late.lastReceived = self.now(10).isoformat()
        self.correlate(late, 22)
        self.dispatcher(22).run_once()
        self.assertEqual(len(self.sent), before)
        self.assertEqual(self.incidents()[0].status, "resolved")


class NotificationExamplesTest(NotificationCase):
    def test_core_target_and_lab_validate_preview_and_preserve_source_routes(self):
        import json
        from pathlib import Path
        from keep.api.bl.incident_provisioning import Candidate, IncidentProvisioning
        from keep.api.core.incident_contract import read_yaml
        from keep.api.models.db.tenant import Tenant
        root = Path(__file__).resolve().parents[1] / "config"
        variants = {}
        for name in ("incident-core.lab", "incident-core.target"):
            directory = root / name
            candidate = Candidate.load(read_yaml(directory / "bundle.yaml"), directory, "keep")
            variants[name] = candidate
        target, lab = variants["incident-core.target"], variants["incident-core.lab"]
        source = json.loads((root / "incident-core.target/source-routes.json").read_text())["routes"]
        destinations = {item["id"]: item for item in target.bundle["destinations"]}
        self.assertEqual(target.bundle["correlation"], lab.bundle["correlation"])
        self.assertEqual(target.bundle["presentations"], lab.bundle["presentations"])
        for route, original in zip(target.bundle["routes"], source):
            self.assertEqual({destinations[ref]["options"]["channel_id"] for ref in route["destination_refs"]}, {original["channel"]})
            self.assertEqual(route["after_silence"], "current_active")
            self.assertEqual(route["initial_delivery"], "active_only")
        self.assertEqual(len(target.bundle["routes"]), len(source))
        self.assertEqual(target.bundle["adoptions"], [])
        with Session(self.engine) as session:
            if not session.get(Tenant, "keep"):
                session.add(Tenant(id="keep", name="Isolated target preview"))
                session.commit()
        service = IncidentProvisioning("keep")
        preview = service.preview(target)
        self.assertTrue(all(item["operation"] == "create" for item in preview["changes"]))
        self.assertIsNone(service.status()["active_digest"])

    def test_two_iac_bundles_select_different_teams_transports_contacts_and_actions(self):
        from pathlib import Path
        from keep.api.bl.incident_provisioning import Candidate
        from keep.api.core.incident_contract import read_yaml
        from keep.api.core.incident_notifications import canonical, choose_routes, ready
        from keep.api.models.db.incident import Incident
        examples = Path(__file__).resolve().parents[1] / "config/incident-notifications.example"
        values = []
        for variant in ("a", "b"):
            directory = examples / variant
            candidate = Candidate.load(read_yaml(directory / "bundle.yaml"), directory, "keep")
            snapshot = {"bundle": candidate.bundle, "digest": candidate.digest, "documents": candidate.documents}
            team = candidate.documents["access"]["teams"][0]["id"]
            fields = {"cluster": "local", "namespace": "service", "workload": "catalog", "kind": "workload"}
            incident = Incident(tenant_id="keep", team_id=team, status="firing", severity=3,
                normalization_context={"team_id": team, "normalized": fields, "normalization": {"fields": {
                    name: {"known": True} for name in fields}}})
            view = canonical(incident, self.now(0))
            routes, reason = choose_routes(snapshot, incident, "incident.created", view)
            self.assertIsNone(reason)
            destinations = [item for item in candidate.bundle["destinations"] if item["team_id"] == team]
            payload = ready(incident, view, snapshot, routes[0], destinations[0], identifier=uuid4(), event_id=uuid4(),
                event_type="incident.created", occurred_at=self.now(0), projection_revision=1)
            values.append((team, {item["kind"] for item in candidate.bundle["transports"]},
                {item["command"] for item in payload["actions"]}, payload["contact_refs"]))
            self.assertEqual(payload["title"], "workload: catalog")
            self.assertTrue(all(reference.startswith(team) for reference in payload["contact_refs"]))
        self.assertNotEqual(values[0][0], values[1][0])
        self.assertEqual(values[0][1], {"http_json"})
        self.assertEqual(values[1][1], {"http_json", "mattermost"})
        self.assertNotEqual(values[0][2], values[1][2])
