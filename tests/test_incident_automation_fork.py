"""Task 28: durable SLA decisions and canonical execution guards."""

from datetime import datetime, timedelta
import logging
from unittest.mock import patch

from sqlmodel import Session, select

from keep.api.core.incident_configuration import configuration_scope
from keep.api.core import incident_lifecycle as life
from keep.api.models.db.incident import Incident
from tests.test_incident_correlation_fork import CorrelationCase


class AutomationCase(CorrelationCase):
    def setUp(self):
        import keep.api.core.incident_automation  # noqa: F401
        from keep.api.logging import WorkflowDBHandler, WorkflowContextFilter
        root = logging.getLogger()
        handlers = [handler for handler in root.handlers if isinstance(handler, WorkflowDBHandler)]
        # A process-global log timer must not outlive a per-test StaticPool/schema.
        # Keep the real handler/storage, but flush synchronously before DB teardown.
        for handler in handlers:
            handler.close()
        super().setUp()
        if not handlers:
            handler = WorkflowDBHandler(flush_interval=3600)
            handler.close()
            handler.addFilter(WorkflowContextFilter())
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            root.addHandler(handler)
            self.addCleanup(root.removeHandler, handler)
            handlers = [handler]
        self.log_handlers = handlers
        for handler in handlers:
            self.addCleanup(handler.flush)
        workflow_logger = logging.getLogger("keep.workflowmanager.workflow")
        self.addCleanup(workflow_logger.setLevel, workflow_logger.level)
        workflow_logger.setLevel(logging.INFO)
        self.document = {"workflow": {"id": "operate", "triggers": [{"type": "manual"}], "actions": [
            {"name": "ordinary", "notification": False, "provider": {"type": "mock", "with": {"value": "record"}}},
            {"name": "notify", "notification": True, "provider": {"type": "mock", "with": {"value": "send"}}},
        ]}}
        self.bundle["workflows"] = [{"id": "operate", "artifact": self.artifact("operate.yaml", self.document)}]
        self.bundle["automation"] = [{"id": "sla", "team_ids": ["alpha", "beta"], "sla_start": "last_reopen",
            "ack_deadline_seconds": 10, "stop_on": ["acknowledged", "resolved"], "on_reopen": "reset",
            "levels": [{"id": "first", "after_seconds": 10, "workflow_refs": ["operate"],
                        "repeat_every_seconds": 5, "repeat_limit": 0},
                       {"id": "second", "after_seconds": 30, "workflow_refs": ["operate"]}]}]
        self.bundle["correlation"][0]["automation_ref"] = "sla"
        self.bundle["correlation"][0]["window_seconds"] = 600
        self.bundle["lifecycle"][0]["reopen"] = dict(mode="reopen", within_seconds=600, ack="reset", assignee="preserve")
        self.apply()

    def now(self, seconds):
        return self.origin + timedelta(seconds=seconds)

    def worker(self):
        from keep.api.core.incident_automation import IncidentAutomationWorker
        return IncidentAutomationWorker(self.engine)

    def tick(self, seconds, *, execute=False):
        with patch("keep.api.core.incident_automation.datetime", wraps=datetime) as clock, \
             patch("keep.api.bl.silences_evaluator.utc_now", return_value=self.now(seconds)):
            clock.utcnow.return_value = self.now(seconds)
            return self.worker().run_once(now=self.now(seconds), execute=execute)

    def silence(self, *, end=20):
        from uuid import uuid4
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CreateSilenceCommand, utc_string
        from keep.api.models.db.user import User
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        actor = AuthenticatedEntity(tenant_id="tenant", email="engineer@example.org", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            session.add(User(tenant_id="tenant", username=actor.email, password_hash="unused", role="responder"))
            session.commit()
            command = CreateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
                "team_id": "alpha", "selector": {"kind": "incident", "incident_ids": [str(identifier)]},
                "starts_at": None, "ends_at": utc_string(self.now(end)), "comment": "Automation verification", "correlation_id": None})
            SilencesBL(session, actor, self.origin).create(command)

    def ticket(self):
        self.document["workflow"]["actions"] = [{"name": "ordinary", "notification": False,
            "provider": {"type": "mock", "with": {"ticket_id": "T/1"}}}]
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.bundle["automation"][0]["ticket"] = {"workflow_ref": "operate", "url_template": "https://tickets.example.org/{{ actions.ordinary.results.ticket_id }}"}
        self.apply()

    def manual_link(self, value="https://manual.example.org/T-1"):
        from keep.api.models.db.alert import AlertEnrichment
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            session.add(AlertEnrichment(tenant_id="tenant", alert_fingerprint=str(identifier), enrichments={"ticket_url": value, "note": "manual"}))
            session.commit()

    def operations(self):
        from keep.api.models.db.incident_automation import IncidentAutomationOperation
        with Session(self.engine) as session:
            return session.exec(select(IncidentAutomationOperation).order_by(
                IncidentAutomationOperation.due_at, IncidentAutomationOperation.id)).all()

    def change(self, status, seconds):
        with configuration_scope("tenant"), Session(self.engine) as session:
            row, group = life.lock_incident(session, "tenant", self.incidents()[0].id)
            life.transition(session, row, status, at=self.now(seconds), actor="engineer", group=group)
            session.commit()


class IncidentAutomationTest(AutomationCase):
    def test_automation_condition_gates_sla_and_cancels_queued_effects(self):
        from tests.test_event_normalization_fork import field
        self.bundle["normalization"][0]["fields"].append(field("routing_level", "labels.routing_level"))
        self.bundle["automation"][0]["match"] = "normalized.routing_level in ['business_critical', 'mission_critical']"
        self.apply()
        self.correlate(self.event(routing_level="stand"))
        self.tick(10)
        self.assertIsNone(self.incidents()[0].automation_context)
        self.assertEqual(self.operations(), [])
        self.correlate(self.event(routing_level="business_critical"), 11)
        self.tick(21)
        state = self.incidents()[0].automation_context
        self.assertEqual(len(self.operations()), 1)
        self.correlate(self.event(routing_level="stand"), 22)
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(23, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].status, "cancelled")
        self.correlate(self.event(routing_level="business_critical"), 24)
        self.tick(25)
        resumed = self.incidents()[0].automation_context
        self.assertEqual(resumed["chain_id"], state["chain_id"])
        self.assertEqual(resumed["ack_deadline_at"], state["ack_deadline_at"])
        self.assertFalse(resumed["cancelled"])

    def test_deadline_is_durable_and_half_open(self):
        self.correlate(self.event())
        state = self.incidents()[0].automation_context
        self.assertEqual(state["ack_deadline_at"], self.now(10).isoformat())
        self.tick(9)
        self.assertFalse(self.incidents()[0].automation_context["ack_breached"])
        self.tick(10)
        self.assertTrue(self.incidents()[0].automation_context["ack_breached"])
        self.assertEqual(self.incidents()[0].automation_context["level"], "first")
        self.assertEqual(len(self.operations()), 1)

    def test_duplicate_scan_and_reapply_do_not_create_a_second_chain(self):
        self.correlate(self.event())
        self.tick(10)
        before = self.incidents()[0].automation_context
        self.apply()
        self.worker().run_once(now=self.now(10), execute=False)
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.incidents()[0].automation_context["chain_id"], before["chain_id"])

    def test_ack_after_enqueue_cancels_without_provider_call(self):
        self.correlate(self.event())
        self.tick(10)
        self.assertIsNotNone(self.incidents()[0].automation_context["next_due_at"])
        self.change("acknowledged", 10)
        self.assertIsNone(self.incidents()[0].automation_context["next_due_at"])
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(11, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_resolve_reset_reopen_does_not_run_the_old_timer(self):
        self.correlate(self.event())
        self.tick(10)
        old = self.incidents()[0].automation_context["chain_id"]
        self.change("resolved", 11)
        self.correlate(self.event(), 12)
        self.assertNotEqual(self.incidents()[0].automation_context["chain_id"], old)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(22).isoformat())
        self.tick(21)
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_continue_reopen_keeps_cursor_and_deadline(self):
        self.bundle["automation"][0]["on_reopen"] = "continue"
        self.apply()
        self.correlate(self.event())
        self.tick(10)
        old = self.incidents()[0].automation_context["chain_id"]
        self.change("resolved", 11)
        self.correlate(self.event(), 12)
        self.assertEqual(self.incidents()[0].automation_context["chain_id"], old)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(10).isoformat())
        self.tick(14)
        self.assertEqual(len(self.operations()), 1)
        self.tick(15)
        self.assertEqual(len(self.operations()), 2)

    def test_policy_pins_current_episode_and_reschedule_is_explicit(self):
        self.correlate(self.event())
        old = self.incidents()[0].automation_context["policy_version"]
        self.bundle["automation"][0]["ack_deadline_seconds"] = 20
        self.apply()
        self.tick(9)
        self.assertEqual(self.incidents()[0].automation_context["policy_version"], old)
        self.bundle["automation"][0]["on_policy_update"] = "reschedule"
        self.apply()
        self.tick(10)
        state = self.incidents()[0].automation_context
        self.assertNotEqual(state["policy_version"], old)
        self.assertEqual(state["ack_deadline_at"], self.now(20).isoformat())

    def test_offline_scan_coalesces_notifications_and_repeats(self):
        self.correlate(self.event())
        self.tick(35)
        operations = self.operations()
        self.assertEqual(len(operations), 2)
        first = next(row for row in operations if row.level_id == "first")
        self.assertFalse(first.context["notification_allowed"])
        self.assertEqual(first.context["notification_skip_reason"], "superseded_level")
        self.assertEqual(self.incidents()[0].automation_context["level"], "second")

    def test_workflow_runs_once_and_preserves_execution_evidence(self):
        self.correlate(self.event())
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", return_value={}) as send:
            self.tick(10, execute=True)
            self.tick(10, execute=True)
        self.assertEqual(send.call_count, 2)
        operation = self.operations()[0]
        self.assertEqual(operation.status, "success")
        self.assertIsNotNone(operation.execution_id)
        from keep.api.models.db.workflow import WorkflowExecutionLog
        for handler in self.log_handlers:
            handler.flush()
        with Session(self.engine) as session:
            self.assertTrue(session.exec(select(WorkflowExecutionLog).where(
                WorkflowExecutionLog.workflow_execution_id == operation.execution_id)).all())

    def test_canonical_team_change_cancels_pending_operation(self):
        self.correlate(self.event())
        self.tick(10)
        with Session(self.engine) as session:
            row = session.get(Incident, self.incidents()[0].id)
            row.team_id = "beta"
            session.add(row)
            session.commit()
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(11, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_active_policy_is_used_by_the_next_same_id_episode(self):
        self.correlate(self.event())
        self.bundle["automation"][0]["ack_deadline_seconds"] = 25
        self.apply()
        self.change("resolved", 1)
        self.correlate(self.event(), 2)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(27).isoformat())

    def test_created_sla_origin_survives_reset_reopen(self):
        self.bundle["automation"][0]["sla_start"] = "created"
        self.apply()
        self.correlate(self.event())
        self.change("resolved", 1)
        self.correlate(self.event(), 2)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(10).isoformat())

    def test_ack_is_optional_stop_and_breach_survives_late_ack(self):
        self.bundle["automation"][0]["stop_on"] = ["resolved"]
        self.apply()
        self.correlate(self.event())
        self.change("acknowledged", 11)
        self.tick(15)
        state = self.incidents()[0].automation_context
        self.assertTrue(state["ack_breached"])
        self.assertIsNone(state["stopped_reason"])
        self.assertEqual(len(self.operations()), 1)

    def test_exact_deadline_ack_prevents_breach_and_send(self):
        self.correlate(self.event())
        self.change("acknowledged", 10)
        self.tick(10)
        self.assertFalse(self.incidents()[0].automation_context["ack_breached"])
        self.assertEqual(self.operations(), [])

    def test_cancel_policy_update_cancels_existing_jobs(self):
        self.correlate(self.event())
        self.tick(10)
        self.bundle["automation"][0]["on_policy_update"] = "cancel"
        self.apply()
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(11, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].result["reason"], "policy_cancelled")

    def test_workflow_revision_update_invalidates_the_queued_binding(self):
        self.correlate(self.event())
        self.tick(10)
        self.document["workflow"]["actions"][0]["provider"]["with"]["value"] = "changed"
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(11, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].result["reason"], "workflow_changed")

    def test_old_pending_level_notifications_are_superseded_after_a_pause(self):
        self.correlate(self.event())
        self.tick(10)
        calls = []
        def send(**kwargs):
            calls.append(kwargs["value"])
            return {}
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=send):
            self.tick(35, execute=True)
        self.assertEqual(calls.count("record"), 2)
        self.assertEqual(calls.count("send"), 1)

    def test_old_pending_repeat_notifications_are_coalesced(self):
        self.correlate(self.event())
        self.tick(10)
        calls = []
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=lambda **kw: calls.append(kw["value"]) or {}):
            self.tick(26, execute=True)
        self.assertEqual(calls.count("record"), 2)
        self.assertEqual(calls.count("send"), 1)
        self.assertEqual(max(row.ordinal for row in self.operations()), 3)

    def test_silence_runs_ordinary_action_and_skips_notification(self):
        self.correlate(self.event())
        self.silence()
        calls = []
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=lambda **kw: calls.append(kw["value"]) or {}):
            self.tick(10, execute=True)
        self.assertEqual(calls, ["record"])
        self.assertTrue(self.incidents()[0].automation_context["ack_breached"])
        self.assertEqual(self.operations()[0].result["steps"]["notify"]["reason"], "silenced")

    def test_silence_added_after_enqueue_is_checked_before_send(self):
        self.correlate(self.event())
        self.tick(10)
        self.silence()
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", return_value={}) as send:
            self.tick(11, execute=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.operations()[0].result["steps"]["notify"]["reason"], "silenced")

    def test_silenced_enqueued_send_is_not_replayed_after_expiry(self):
        self.correlate(self.event())
        self.silence(end=12)
        self.tick(10)
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", return_value={}) as send:
            self.tick(13, execute=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.operations()[0].result["steps"]["notify"]["reason"], "silenced")

    def test_ack_during_workflow_stops_the_next_action(self):
        self.correlate(self.event())
        def ordinary(**kwargs):
            self.change("acknowledged", 10)
            return {}
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=ordinary) as send:
            self.tick(10, execute=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.operations()[0].status, "cancelled")
        self.assertIn(self.operations()[0].result["reason"], ("acknowledged", "incident_revision_changed"))

    def test_restart_before_provider_reclaims_with_the_same_execution_id(self):
        self.correlate(self.event())
        self.tick(10)
        old = self.worker().claim(self.operations()[0].id, self.now(10))
        new = self.worker().claim(old.id, self.now(311))
        self.assertIsNotNone(new)
        self.assertNotEqual(old.token, new.token)
        self.assertEqual(old.execution_id, new.execution_id)

    def test_restart_after_provider_claim_is_uncertain_and_never_replayed(self):
        self.correlate(self.event())
        self.tick(10)
        worker = self.worker()
        claimed = worker.claim(self.operations()[0].id, self.now(10))
        from keep.api.models.db.incident_automation import IncidentAutomationOperation
        with Session(self.engine) as session:
            row = session.get(IncidentAutomationOperation, claimed.id)
            row.effect_started = True
            session.add(row)
            session.commit()
        self.assertIsNone(worker.claim(claimed.id, self.now(311)))
        self.assertEqual(self.operations()[0].status, "uncertain")
        self.assertIsNone(worker.claim(claimed.id, self.now(312)))

    def test_ticket_workflow_and_url_template_execute_once(self):
        self.ticket()
        self.correlate(self.event())
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", return_value={"ticket_id": "T/1"}) as send:
            self.tick(0, execute=True)
            self.tick(0, execute=True)
        self.assertEqual(send.call_count, 1)
        from keep.api.core.incident_automation import incident_metadata
        with Session(self.engine) as session:
            row = session.get(Incident, self.incidents()[0].id)
            self.assertEqual(incident_metadata(session, row).enrichments["ticket_url"], "https://tickets.example.org/T%2F1")
        self.assertEqual(self.operations()[0].status, "success")

    def test_existing_manual_ticket_skips_ticket_creation(self):
        self.ticket()
        self.correlate(self.event())
        self.manual_link()
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify") as send:
            self.tick(0, execute=True)
        send.assert_not_called()
        self.assertEqual(self.operations()[0].result["reason"], "ticket_link_exists")

    def test_manual_ticket_added_during_provider_wins(self):
        self.ticket()
        self.correlate(self.event())
        def create(**kwargs):
            self.manual_link()
            return {"ticket_id": "T/1"}
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=create):
            self.tick(0, execute=True)
        from keep.api.core.incident_automation import incident_metadata
        with Session(self.engine) as session:
            row = session.get(Incident, self.incidents()[0].id)
            self.assertEqual(incident_metadata(session, row).enrichments["ticket_url"], "https://manual.example.org/T-1")

    def test_ticket_cannot_enrich_canonical_state(self):
        self.bundle["automation"][0]["ticket"] = {"workflow_ref": "operate", "url_template": "https://tickets.example.org/T-1", "enrichment_key": "team_id"}
        with self.assertRaisesRegex(ValueError, "reserved incident field"):
            self.candidate()

    def test_automation_is_server_owned_projection(self):
        self.correlate(self.event())
        from keep.api.models.incident import IncidentDto
        row = self.incidents()[0]
        row.set_enrichments({"automation": {"policy_id": "fake"}, "automation_context": {"policy": "fake"}})
        dto = IncidentDto.from_db_incident(row, with_silences=False)
        self.assertEqual(dto.automation["policy_id"], "sla")
        self.assertNotIn("bindings", dto.automation)

    def test_runtime_examples_have_distinct_teams_deadlines_and_multiple_transport_refs(self):
        from pathlib import Path
        from keep.api.bl.incident_provisioning import Candidate
        root = Path(__file__).resolve().parents[1] / "config/incident-automation.example"
        candidates = [Candidate.from_file(root / name / "bundle.yaml", "keep") for name in ("a", "b")]
        self.assertTrue(set(candidates[0].bundle["automation"][0]["team_ids"]).isdisjoint(candidates[1].bundle["automation"][0]["team_ids"]))
        self.assertNotEqual(candidates[0].bundle["automation"][0]["ack_deadline_seconds"], candidates[1].bundle["automation"][0]["ack_deadline_seconds"])
        self.assertEqual({item["transport_ref"] for item in candidates[0].bundle["destinations"]}, {"primary", "secondary"})

    def test_destinations_and_contacts_are_filtered_by_canonical_team(self):
        self.bundle["transports"] = [{"id": "http", "kind": "http_json", "adapter_ref": "http-json-v1",
            "endpoint": "https://receiver.example.org", "auth_ref": None, "capabilities": {"update": False, "actions": False, "receipts": False}}]
        self.bundle["destinations"] = [{"id": team, "team_id": team, "transport_ref": "http", "options": {"path": "/events"}} for team in ("alpha", "beta")]
        self.bundle["contacts"] = [{"id": team, "team_id": team, "label": team, "addresses": [{"transport_ref": "http", "address": team}]} for team in ("alpha", "beta")]
        self.bundle["automation"][0]["levels"][0].update(destination_refs=["alpha", "beta"], contact_refs=["alpha", "beta"])
        self.bundle["automation"][0]["reminder"] = {"every_seconds": 5, "destination_refs": ["alpha", "beta"], "contact_refs": ["alpha", "beta"]}
        self.apply()
        self.correlate(self.event())
        self.tick(11)
        destinations = [row for row in self.operations() if row.target_kind == "destination"]
        self.assertEqual({row.target_ref for row in destinations}, {"alpha"})
        self.assertTrue(all(row.context["contact_refs"] == ["alpha"] for row in self.operations()))
        reminders = [row for row in destinations if row.kind == "reminder"]
        self.assertEqual([row.ordinal for row in reminders], [2])
        self.assertEqual(reminders[0].status, "awaiting_dispatch")

    def test_finite_repeat_limit_stops_and_next_level_owns_exact_boundary(self):
        self.bundle["automation"][0]["levels"][0]["repeat_limit"] = 2
        self.apply()
        self.correlate(self.event())
        for second in (10, 15, 20, 29, 30):
            self.tick(second)
        first = [row.ordinal for row in self.operations() if row.level_id == "first"]
        self.assertEqual(first, [0, 1])
        self.assertEqual(self.incidents()[0].automation_context["level"], "second")

    def test_snapshot_policy_update_is_visible_in_preview_without_rescheduling(self):
        self.correlate(self.event())
        before = self.incidents()[0].automation_context
        self.bundle["automation"][0]["on_policy_update"] = "reschedule"
        plan = self.service.preview(self.candidate())
        self.assertEqual(plan["automation_impact"]["affected_open_incidents"], 1)
        self.assertEqual(plan["automation_impact"]["policies"], {"sla": "reschedule"})
        self.assertEqual(self.incidents()[0].automation_context, before)

    def test_manual_metadata_during_provider_enrichment_is_preserved(self):
        self.ticket()
        self.document["workflow"]["actions"][0]["provider"]["with"]["enrich_alert"] = [{"key": "ticket_url", "value": "https://generated.example.org/T-1"}]
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        self.correlate(self.event())
        def create(**kwargs):
            self.manual_link()
            return {"ticket_id": "T/1"}
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=create):
            self.tick(0, execute=True)
        from keep.api.core.incident_automation import incident_metadata
        with Session(self.engine) as session:
            row = session.get(Incident, self.incidents()[0].id)
            self.assertEqual(incident_metadata(session, row).enrichments["ticket_url"], "https://manual.example.org/T-1")
        self.assertEqual(self.operations()[0].status, "success")

    def test_notification_retry_rechecks_ack_before_the_second_attempt(self):
        self.document["workflow"]["actions"][1]["on-failure"] = {"retry": {"count": 1, "interval": 0}}
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        self.correlate(self.event())
        calls = []
        def send(**kwargs):
            calls.append(kwargs["value"])
            if kwargs["value"] == "send":
                self.change("acknowledged", 10)
                raise RuntimeError("receiver failure")
            return {}
        with patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=send):
            self.tick(10, execute=True)
        self.assertEqual(calls, ["record", "send"])
        self.assertEqual(self.operations()[0].status, "cancelled")

    def test_viewer_and_foreign_team_cannot_ack_an_automated_incident(self):
        from fastapi import HTTPException
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        self.correlate(self.event())
        for role, team in (("viewer", "alpha"), ("responder", "beta")):
            actor = AuthenticatedEntity(tenant_id="tenant", email="viewer@example.org", role=role,
                teams=frozenset({team}), visible_teams=frozenset({team}))
            with Session(self.engine) as session, self.assertRaises(HTTPException):
                life.lock_incident(session, "tenant", self.incidents()[0].id, actor)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_automated_actions_require_explicit_notification_classification(self):
        self.document["workflow"]["actions"][1].pop("notification")
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        with self.assertRaisesRegex(ValueError, "explicitly declare"):
            self.candidate()

    def test_unknown_workflow_strategy_is_rejected_before_apply(self):
        self.document["workflow"]["strategy"] = "unsupported"
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        with self.assertRaisesRegex(ValueError, "strategy"):
            self.candidate()

    def test_running_workflow_checks_current_level_even_without_another_scan(self):
        self.correlate(self.event())
        self.tick(10)
        worker = self.worker()
        claimed = worker.claim(self.operations()[0].id, self.now(10))
        calls = []
        with patch("keep.api.core.incident_automation.datetime", wraps=datetime) as clock, \
             patch("keep.providers.mock_provider.mock_provider.MockProvider._notify", side_effect=lambda **kw: calls.append(kw["value"]) or {}):
            clock.utcnow.return_value = self.now(31)
            worker.execute(claimed, self.now(31))
        self.assertEqual(calls, ["record"])
        self.assertEqual(self.operations()[0].result["steps"]["notify"]["reason"], "superseded_level")

    def test_scheduler_submits_to_the_existing_pool_with_iac_cadence(self):
        from unittest.mock import Mock
        from keep.workflowmanager.workflowscheduler import WorkflowScheduler
        self.correlate(self.event())
        scheduler = WorkflowScheduler.__new__(WorkflowScheduler)
        scheduler._automation_next_scan = 0
        scheduler.futures = set()
        scheduler.MAX_WORKERS = 20
        scheduler.executor = Mock()
        self.bundle["dispatch"] = {"scan_interval_seconds": 17, "batch_size": 2}
        self.apply()
        with patch("keep.workflowmanager.workflowscheduler.datetime", wraps=datetime) as clock, \
             patch("keep.workflowmanager.workflowscheduler.time.monotonic", return_value=100):
            clock.utcnow.return_value = self.now(10)
            scheduler._handle_incident_automation()
            scheduler._handle_incident_automation()
        self.assertEqual(scheduler._automation_next_scan, 117)
        self.assertEqual(scheduler.executor.submit.call_count, 1)
        self.assertEqual(self.operations()[0].status, "running")

    def test_default_nonparallel_retry_waits_for_the_running_workflow(self):
        self.correlate(self.event())
        self.tick(10)
        worker = self.worker()
        old = worker.claim(self.operations()[0].id, self.now(10))
        self.tick(15)
        new = next(row for row in self.operations() if row.id != old.id)
        self.assertIsNone(worker.claim(new.id, self.now(15)))
        self.assertEqual(next(row for row in self.operations() if row.id == new.id).status, "pending")

    def test_parallel_workflow_allows_distinct_operations(self):
        self.document["workflow"]["strategy"] = "parallel"
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        self.correlate(self.event())
        self.tick(10)
        old = self.worker().claim(self.operations()[0].id, self.now(10))
        self.tick(15)
        new = next(row for row in self.operations() if row.id != old.id)
        self.assertIsNotNone(self.worker().claim(new.id, self.now(15)))

    def test_nonparallel_without_retry_skips_overlap(self):
        self.document["workflow"]["strategy"] = "nonparallel"
        self.bundle["workflows"][0]["artifact"] = self.artifact("operate.yaml", self.document)
        self.apply()
        self.correlate(self.event())
        self.tick(10)
        old = self.worker().claim(self.operations()[0].id, self.now(10))
        self.tick(15)
        new = next(row for row in self.operations() if row.id != old.id)
        self.assertIsNone(self.worker().claim(new.id, self.now(15)))
        current = next(row for row in self.operations() if row.id == new.id)
        self.assertEqual((current.status, current.result["reason"]), ("skipped", "workflow_already_running"))

    def test_active_reopen_policy_can_change_continue_to_reset(self):
        self.bundle["automation"][0]["on_reopen"] = "continue"
        self.apply()
        self.correlate(self.event())
        self.bundle["automation"][0]["on_reopen"] = "reset"
        self.bundle["automation"][0]["ack_deadline_seconds"] = 25
        self.apply()
        self.change("resolved", 1)
        self.correlate(self.event(), 2)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(27).isoformat())

    def test_active_reopen_policy_can_change_reset_to_continue(self):
        self.correlate(self.event())
        self.bundle["automation"][0]["on_reopen"] = "continue"
        self.bundle["automation"][0]["ack_deadline_seconds"] = 25
        self.apply()
        self.change("resolved", 1)
        self.correlate(self.event(), 2)
        self.assertEqual(self.incidents()[0].automation_context["ack_deadline_at"], self.now(25).isoformat())
