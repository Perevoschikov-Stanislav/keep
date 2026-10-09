"""Task 27: durable lifecycle, timing, reopening and operator state."""

from datetime import timedelta
from unittest.mock import patch

from sqlmodel import Session, select

from keep.api.models.db.incident import Incident
from keep.api.models.db.alert import AlertAudit
from keep.api.models.action_type import ActionType
from keep.api.models.db.incident import IncidentStatus
from keep.api.bl.incidents_bl import IncidentBl
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from tests.test_incident_correlation_fork import CorrelationCase


class IncidentLifecycleTest(CorrelationCase):
    def save(self, event, seconds=0):
        # Match ingestion: a new provider version disposes temporary ACK/resolve
        # enrichments before correlation; saving a replay does not call this.
        from keep.api.bl.enrichments_bl import EnrichmentsBl
        super().save(event, seconds)
        with Session(self.engine) as session:
            EnrichmentsBl("tenant", db=session).dispose_enrichments(event.fingerprint)
        return event

    def actor(self, *, role="responder", team="alpha"):
        return AuthenticatedEntity(tenant_id="tenant", email="engineer@example.org", role=role,
            teams=frozenset({team}), visible_teams=frozenset({team}))

    def change(self, status, seconds, *, entity=None, revision=None, identifier=None):
        from keep.api.models.incident import IncidentDto
        incident_id = identifier or self.incidents()[0].id
        with Session(self.engine) as session, patch("keep.api.bl.incidents_bl.datetime", wraps=__import__("datetime").datetime) as clock:
            clock.utcnow.return_value = self.origin + timedelta(seconds=seconds)
            with patch.object(IncidentBl, "_IncidentBl__postprocess_incident_change",
                              side_effect=lambda row: IncidentDto.from_db_incident(row, session=session, with_silences=False)):
                return IncidentBl("tenant", session).change_status(incident_id, IncidentStatus(status), entity or self.actor(),
                                                                  expected_revision=revision)

    def audits(self):
        with Session(self.engine) as session:
            return session.exec(select(AlertAudit).where(AlertAudit.action == ActionType.INCIDENT_STATUS_CHANGE.value)).all()

    def configure(self, *, clock="receive_time", ack="reset", assignee="preserve", mode="reopen", within=60,
                  window=60, threshold=2, reset=60):
        policy = self.bundle["lifecycle"][0]
        policy.update(clock=clock, reopen=dict(mode=mode, within_seconds=within, ack=ack, assignee=assignee),
                      flapping=dict(enabled=True, window_seconds=window, transition_threshold=threshold,
                                    reset_after_seconds=reset))
        self.apply()

    def operator_state(self):
        with Session(self.engine) as session:
            incident = session.exec(select(Incident)).one()
            incident.assignee = "engineer@example.org"
            incident.user_summary = "Keep this note"
            session.add(incident)
            session.commit()

    def test_reopen_reuses_id_preserves_notes_and_assignee(self):
        self.configure()
        self.correlate(self.event())
        self.operator_state()
        old = self.incidents()[0]
        self.correlate(self.event(status="resolved"), 10)
        self.correlate(self.event(), 69)
        rows = self.incidents()
        self.assertEqual([row.id for row in rows], [old.id])
        self.assertEqual(rows[0].status, "firing")
        self.assertIsNone(rows[0].end_time)
        self.assertEqual(rows[0].assignee, "engineer@example.org")
        self.assertEqual(rows[0].user_summary, "Keep this note")
        self.assertEqual(rows[0].lifecycle_context["episode_start"], (self.origin + timedelta(seconds=69)).isoformat())

    def test_reopen_upper_boundary_creates_linked_new_id(self):
        self.configure()
        self.correlate(self.event())
        old = self.incidents()[0]
        self.correlate(self.event(status="resolved"), 10)
        self.correlate(self.event(), 70)
        rows = self.incidents()
        self.assertEqual(len(rows), 2)
        new = next(row for row in rows if row.id != old.id)
        self.assertEqual(new.same_incident_in_the_past_id, old.id)

    def test_flapping_counts_phase_changes_and_ignores_duplicate_delivery(self):
        self.configure()
        self.correlate(self.event())
        resolved = self.event(status="resolved")
        self.correlate(resolved, 10)
        self.correlate(resolved, save=False)
        self.correlate(self.event(status="resolved"), 11)
        self.correlate(self.event(), 12)
        state = self.incidents()[0].lifecycle_context
        self.assertEqual(state["flapping"]["transition_count"], 2)
        self.assertTrue(state["flapping"]["active"])

    def test_late_event_time_cannot_revert_latest_state(self):
        self.configure(clock="event_time")
        first = self.event()
        first.lastReceived = self.origin.isoformat()
        self.correlate(first)
        resolved = self.event(status="resolved")
        resolved.lastReceived = (self.origin + timedelta(seconds=20)).isoformat()
        self.correlate(resolved, 21)
        late = self.event()
        late.lastReceived = (self.origin + timedelta(seconds=10)).isoformat()
        self.correlate(late, 22)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0].status, "resolved")
        self.assertEqual(late.correlation["decisions"][0]["reason"], "history_only")

    def test_late_event_cannot_escape_cursor_with_changed_group_values(self):
        self.configure(clock="event_time")
        first = self.event()
        first.lastReceived = (self.origin + timedelta(seconds=20)).isoformat()
        self.correlate(first, 20)
        late = self.event(workload="old-workload")
        late.lastReceived = (self.origin + timedelta(seconds=10)).isoformat()
        self.correlate(late, 21)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(late.correlation["decisions"][0]["reason"], "history_only")

    def test_quiet_reset_exact_boundary(self):
        self.configure(reset=20)
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 10)
        self.correlate(self.event(), 11)
        self.assertTrue(self.incidents()[0].lifecycle_context["flapping"]["active"])
        self.correlate(self.event(), 31)
        state = self.incidents()[0].lifecycle_context["flapping"]
        self.assertFalse(state["active"])
        self.assertEqual(state["transition_count"], 0)

    def test_partial_resolution_uses_all_members(self):
        self.configure()
        self.correlate(self.event())
        self.correlate(self.event("p-2"), 1)
        self.correlate(self.event(status="resolved"), 2)
        state = self.incidents()[0].lifecycle_context
        self.assertEqual(state["members"], {"total": 2, "resolved": 1, "active": 1, "resolution": "partial"})
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_ack_then_auto_resolve_keeps_operator_assignee(self):
        self.configure(ack="preserve")
        self.correlate(self.event())
        self.change("acknowledged", 2)
        self.correlate(self.event(status="resolved"), 3)
        self.assertEqual(self.incidents()[0].assignee, "engineer@example.org")
        self.correlate(self.event(), 4)
        row = self.incidents()[0]
        self.assertEqual(row.status, "acknowledged")
        self.assertEqual(row.lifecycle_context["ack_by"], "engineer@example.org")
        self.assertEqual(len(self.audits()), 3)
        self.assertEqual(row.lifecycle_context["flapping"]["transition_count"], 2)

    def test_ack_reset_and_explicit_assignee_clear(self):
        self.configure(assignee="clear")
        self.correlate(self.event())
        self.change("acknowledged", 1)
        self.correlate(self.event(status="resolved"), 2)
        self.correlate(self.event(), 3)
        row = self.incidents()[0]
        self.assertEqual(row.status, "firing")
        self.assertIsNone(row.assignee)
        self.assertNotIn("ack_by", row.lifecycle_context)

    def test_manual_resolve_by_other_user_never_steals_assignee(self):
        self.correlate(self.event())
        self.change("acknowledged", 1)
        entity = self.actor()
        entity.email = "other@example.org"
        self.change("resolved", 2, entity=entity)
        self.assertEqual(self.incidents()[0].assignee, "engineer@example.org")
        self.assertEqual(len(self.audits()), 2)

    def test_noop_status_has_no_audit_or_revision_increment(self):
        self.correlate(self.event())
        self.change("acknowledged", 1)
        self.change("acknowledged", 2)
        self.assertEqual(self.incidents()[0].lifecycle_context["revision"], 1)
        self.assertEqual(len(self.audits()), 1)

    def test_stale_revision_rejected_without_changing_alerts(self):
        from fastapi import HTTPException
        self.correlate(self.event())
        self.change("acknowledged", 1)
        with self.assertRaises(HTTPException) as error:
            self.change("resolved", 2, revision=0)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.incidents()[0].status, "acknowledged")
        self.assertEqual(len(self.audits()), 1)

    def test_service_enforces_role_and_team_without_router(self):
        from fastapi import HTTPException
        self.correlate(self.event())
        for entity, expected in ((self.actor(role="viewer"), 403), (self.actor(team="beta"), 404)):
            with self.subTest(role=entity.role, teams=entity.teams), self.assertRaises(HTTPException) as error:
                self.change("resolved", 2, entity=entity)
            self.assertEqual(error.exception.status_code, expected)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_service_rejects_cross_team_alert_history(self):
        from fastapi import HTTPException
        self.correlate(self.event())
        self.save(self.event(team="beta"), 1)
        with self.assertRaises(HTTPException) as error:
            self.change("resolved", 2)
        self.assertEqual(error.exception.status_code, 404)

    def test_responder_cannot_delete_via_status(self):
        from fastapi import HTTPException
        self.correlate(self.event())
        with self.assertRaises(HTTPException) as error:
            self.change("deleted", 1)
        self.assertEqual(error.exception.status_code, 403)

    def test_new_incident_never_inherits_ack_but_preserves_assignee_when_configured(self):
        self.configure(mode="new_incident", within=0)
        self.correlate(self.event())
        self.change("acknowledged", 1)
        old = self.incidents()[0]
        self.correlate(self.event(status="resolved"), 2)
        self.correlate(self.event(), 3)
        new = next(row for row in self.incidents() if row.id != old.id)
        self.assertEqual(new.status, "firing")
        self.assertEqual(new.assignee, "engineer@example.org")
        self.assertNotIn("ack_by", new.lifecycle_context)
        self.assertTrue(new.lifecycle_context["flapping"]["active"])
        self.assertEqual(new.lifecycle_context["group_id"], old.lifecycle_context["group_id"])

    def test_sliding_window_excludes_lower_bound(self):
        self.configure(window=10, reset=100)
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event(), 11)
        state = self.incidents()[0].lifecycle_context["flapping"]
        self.assertEqual(state["transition_count"], 1)
        self.assertFalse(state["active"])

    def test_late_member_does_not_poison_later_full_resolution(self):
        self.configure(clock="event_time")
        def event_at(fp, status, at, received):
            event = self.event(fp, status=status)
            event.lastReceived = (self.origin + timedelta(seconds=at)).isoformat()
            self.correlate(event, received)
        event_at("p-1", "firing", 0, 0)
        event_at("p-2", "firing", 1, 1)
        event_at("p-1", "resolved", 10, 10)
        event_at("p-1", "firing", 9, 11)
        event_at("p-2", "resolved", 12, 12)
        row = self.incidents()[0]
        self.assertEqual(row.status, "resolved")
        self.assertEqual(row.lifecycle_context["members"]["resolution"], "full")

    def test_equal_source_time_conflict_is_history_only(self):
        self.configure(clock="event_time")
        first = self.event()
        first.lastReceived = self.origin.isoformat()
        self.correlate(first)
        conflict = self.event(status="resolved")
        conflict.lastReceived = first.lastReceived
        self.correlate(conflict, 1)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(conflict.correlation["decisions"][0]["reason"], "history_only")

    def test_existing_member_resolves_after_active_match_no_longer_matches(self):
        self.bundle["correlation"][0]["match"] = "status == 'firing'"
        self.apply()
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 1)
        self.assertEqual(self.incidents()[0].status, "resolved")

    def test_policy_update_preview_keeps_existing_policy_and_state_pinned(self):
        self.configure()
        self.correlate(self.event())
        old = self.incidents()[0]
        self.bundle["lifecycle"][0]["flapping"]["transition_threshold"] = 4
        preview = self.service.preview(self.candidate())
        self.assertEqual(preview["correlation_impact"]["affected_open_incidents"], 1)
        self.assertFalse(preview["correlation_impact"]["rewrite_history"])
        self.apply()
        self.correlate(self.event(status="resolved"), 1)
        retained = next(row for row in self.incidents() if row.id == old.id)
        self.assertEqual(retained.correlation_context["lifecycle"]["flapping"]["transition_threshold"], 2)
        self.assertEqual(retained.status, "resolved")

    def test_state_and_audit_survive_delivery_failure_and_fresh_session(self):
        self.configure()
        self.correlate(self.event())
        self.published.side_effect = RuntimeError("Transport unavailable")
        with self.assertRaisesRegex(RuntimeError, "Transport unavailable"):
            self.correlate(self.event(status="resolved"), 1)
        self.assertEqual(self.incidents()[0].status, "resolved")
        self.assertEqual(len(self.audits()), 1)
        self.published.side_effect = None
        self.correlate(self.event(), 2)
        self.assertTrue(self.incidents()[0].lifecycle_context["flapping"]["active"])

    def test_manual_reopening_uses_same_policy_and_resets_episode_clock(self):
        self.configure()
        self.correlate(self.event())
        self.change("resolved", 1)
        self.change("firing", 2)
        row = self.incidents()[0]
        self.assertEqual(row.lifecycle_context["episode"], 2)
        self.assertEqual(row.lifecycle_context["episode_start"], (self.origin + timedelta(seconds=2)).isoformat())
        self.assertIsNone(row.end_time)
        self.assertTrue(row.lifecycle_context["flapping"]["active"])

    def test_manual_reopening_cannot_bypass_new_incident_policy(self):
        from fastapi import HTTPException
        self.correlate(self.event())
        self.change("resolved", 1)
        with self.assertRaises(HTTPException) as error:
            self.change("firing", 2)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.incidents()[0].status, "resolved")

    def test_general_put_cannot_bypass_status_checks_or_forge_lifecycle(self):
        import keep.api.core.db as db
        from fastapi import HTTPException
        from keep.api.models.incident import IncidentDtoIn
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        with self.assertRaises(HTTPException) as error:
            db.update_incident_from_dto_by_id("tenant", identifier, IncidentDtoIn(status="resolved"))
        self.assertEqual(error.exception.status_code, 400)
        db.update_incident_from_dto_by_id("tenant", identifier, IncidentDtoIn(
            user_summary="Manual note", lifecycle_context={"team_id": "alpha", "revision": 999}))
        self.assertEqual(self.incidents()[0].lifecycle_context["revision"], 0)

    def test_audit_failure_rolls_back_status_cursors_and_flapping(self):
        self.configure()
        self.correlate(self.event())
        before = self.incidents()[0].lifecycle_context
        with patch("keep.api.core.db.add_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.correlate(self.event(status="resolved"), 1)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(self.incidents()[0].lifecycle_context, before)
        self.assertEqual(self.audits(), [])

    def silence(self, identifier):
        from uuid import uuid4
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CreateSilenceCommand, utc_string
        from keep.api.models.db.user import User
        with Session(self.engine) as session:
            session.add(User(tenant_id="tenant", username=self.actor().email, password_hash="unused", role="responder"))
            session.commit()
            command = CreateSilenceCommand.parse_obj({"schema_version": 1, "client_request_id": str(uuid4()),
                "team_id": "alpha", "selector": {"kind": "incident", "incident_ids": [str(identifier)]},
                "starts_at": None, "ends_at": utc_string(self.origin + timedelta(hours=1)),
                "comment": "Lifecycle verification", "correlation_id": None})
            SilencesBL(session, self.actor(), self.origin).create(command)

    def silenced(self, identifier, seconds):
        from keep.api.bl.silences_evaluator import SilenceEvaluator
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            return SilenceEvaluator(session, "tenant", self.origin + timedelta(seconds=seconds)).incidents([row])[row.id].silenced

    def test_silence_survives_same_id_reopen_and_state_keeps_transitioning(self):
        self.configure()
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        self.silence(identifier)
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event(), 2)
        self.assertTrue(self.silenced(identifier, 2))
        self.assertTrue(self.incidents()[0].lifecycle_context["flapping"]["active"])
        self.assertEqual(len(self.audits()), 2)

    def test_new_id_does_not_inherit_old_incident_silence(self):
        self.configure(mode="new_incident", within=0)
        self.correlate(self.event())
        old = self.incidents()[0].id
        self.silence(old)
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event(), 2)
        new = next(row for row in self.incidents() if row.id != old)
        self.assertFalse(self.silenced(new.id, 2))
        self.assertTrue(self.silenced(old, 2))

    def test_read_projects_quiet_reset_without_a_background_event(self):
        from keep.api.core import incident_lifecycle as life
        self.configure(reset=20)
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event(), 2)
        with patch.object(life, "datetime", wraps=__import__("datetime").datetime) as clock:
            clock.utcnow.return_value = self.origin + timedelta(seconds=22)
            self.assertFalse(life.project(self.incidents()[0])["flapping"]["active"])
        self.assertTrue(self.incidents()[0].lifecycle_context["flapping"]["active"])

    def test_old_episode_cannot_be_reopened_after_new_id_exists(self):
        from fastapi import HTTPException
        self.configure()
        self.correlate(self.event())
        old = self.incidents()[0].id
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event(), 61)
        with self.assertRaises(HTTPException) as error:
            self.change("firing", 2, identifier=old)
        self.assertEqual(error.exception.status_code, 409)

    def test_delete_is_not_a_firing_resolved_flap(self):
        self.configure()
        self.correlate(self.event())
        self.change("deleted", 1, entity=self.actor(role="admin"))
        self.correlate(self.event(), 2)
        new = next(row for row in self.incidents() if row.status == "firing")
        self.assertEqual(new.lifecycle_context["flapping"]["transition_count"], 0)
        self.assertFalse(new.lifecycle_context["flapping"]["active"])

    def test_event_time_first_member_uses_source_order_not_receive_order(self):
        self.configure(clock="event_time")
        self.bundle["lifecycle"][0]["resolve_on"] = "first_resolved"
        self.apply()
        first = self.event()
        first.lastReceived = self.origin.isoformat()
        self.correlate(first, 20)
        second = self.event("p-2")
        second.lastReceived = (self.origin + timedelta(seconds=1)).isoformat()
        self.correlate(second, 10)
        resolved = self.event(status="resolved")
        resolved.lastReceived = (self.origin + timedelta(seconds=2)).isoformat()
        self.correlate(resolved, 21)
        self.assertEqual(self.incidents()[0].status, "resolved")

    def test_past_version_cannot_create_incident_for_previous_owner(self):
        self.configure(clock="event_time")
        previous = self.event()
        previous.lastReceived = self.origin.isoformat()
        self.save(previous)
        self.save(self.event(team="beta"), 1)
        self.correlate(previous, save=False)
        self.assertEqual(self.incidents(), [])
        self.assertEqual(previous.correlation["decisions"][0]["reason"], "ownership_mismatch")

    def test_general_edit_rechecks_current_owner_under_lock(self):
        import keep.api.core.db as db
        from fastapi import HTTPException
        from keep.api.models.incident import IncidentDtoIn
        self.correlate(self.event(team="beta"))
        with self.assertRaises(HTTPException) as error:
            db.update_incident_from_dto_by_id("tenant", self.incidents()[0].id,
                IncidentDtoIn(assignee="engineer@example.org"), authenticated_entity=self.actor())
        self.assertEqual(error.exception.status_code, 404)

    def test_runtime_examples_enable_distinct_lifecycle_modes(self):
        from pathlib import Path
        from keep.api.bl.incident_provisioning import Candidate
        root = Path(__file__).resolve().parents[1] / "config/incident-lifecycle.example"
        candidates = [Candidate.from_file(root / name / "bundle.yaml", "keep") for name in ("a", "b")]
        self.assertEqual([candidate.bundle["lifecycle"][0]["reopen"]["mode"] for candidate in candidates], ["reopen", "new_incident"])
        self.assertEqual([candidate.bundle["lifecycle"][0]["clock"] for candidate in candidates], ["receive_time", "event_time"])
        self.assertTrue(all(candidate.bundle["lifecycle"][0]["flapping"]["enabled"] for candidate in candidates))

    def test_first_resolved_does_not_immediately_close_reopen_for_new_replica(self):
        self.configure()
        self.bundle["lifecycle"][0]["resolve_on"] = "first_resolved"
        self.apply()
        self.correlate(self.event())
        self.correlate(self.event(status="resolved"), 1)
        self.correlate(self.event("new-replica"), 2)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(self.incidents()[0].lifecycle_context["flapping"]["transition_count"], 2)
        self.correlate(self.event("new-replica", status="resolved"), 3)
        self.assertEqual(self.incidents()[0].status, "resolved")

    def test_resolution_reads_more_than_500_current_members(self):
        from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
        self.configure()
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            for index in range(501):
                event = self.event("resolved-" + str(index), status="resolved")
                row = Alert(tenant_id="tenant", team_id="alpha", fingerprint=event.fingerprint, provider_type="keep",
                            event=event.to_ingestion_dict(), timestamp=self.origin)
                session.add(row)
                session.add(LastAlert(tenant_id="tenant", fingerprint=row.fingerprint, alert_id=row.id,
                                      timestamp=self.origin, first_timestamp=self.origin))
                session.add(LastAlertToIncident(tenant_id="tenant", incident_id=identifier, fingerprint=row.fingerprint))
            session.commit()
        self.correlate(self.event(status="resolved"), 1)
        row = self.incidents()[0]
        self.assertEqual(row.status, "resolved")
        self.assertEqual(row.lifecycle_context["members"], {"total": 502, "resolved": 502, "active": 0, "resolution": "full"})

    def test_workflow_incident_enrichment_uses_canonical_transition_and_preserves_assignee(self):
        from keep.api.bl.enrichments_bl import EnrichmentsBl
        from keep.api.models.incident import IncidentDto
        from keep.api.core.db import get_incident_by_id
        self.configure()
        self.correlate(self.event())
        self.operator_state()
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            EnrichmentsBl("tenant", db=session).enrich_entity(identifier,
                {"status": "resolved", "ticket_url": "https://tracker.example.org/1"},
                ActionType.WORKFLOW_ENRICH, "system", "Workflow resolution", audit_enabled=False,
                expected_team_id="alpha", entity_type="incident")
            row = get_incident_by_id("tenant", identifier, session=session)
            dto = IncidentDto.from_db_incident(row, session=session, with_silences=False)
            self.assertEqual(dto.status.value, "resolved")
            self.assertEqual(dto.assignee, "engineer@example.org")
            self.assertEqual(dto.enrichments["ticket_url"], "https://tracker.example.org/1")
        self.assertEqual(len(self.audits()), 1)

    def test_workflow_rechecks_owner_and_cannot_delete_through_enrichment(self):
        from fastapi import HTTPException
        from keep.api.bl.enrichments_bl import EnrichmentsBl
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        for status, team, expected in (("resolved", "beta", 409), ("deleted", "alpha", 403)):
            with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
                EnrichmentsBl("tenant", db=session).enrich_entity(identifier, {"status": status},
                    ActionType.WORKFLOW_ENRICH, "system", "Bad workflow target", expected_team_id=team, entity_type="incident")
            self.assertEqual(error.exception.status_code, expected)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_incident_response_ignores_legacy_status_and_assignee_shadow(self):
        from keep.api.models.incident import IncidentDto
        self.correlate(self.event())
        row = self.incidents()[0]
        row.set_enrichments({"status": "resolved", "assignee": "forged", "lifecycle": {"revision": 999}})
        dto = IncidentDto.from_db_incident(row, with_silences=False)
        self.assertEqual(dto.status.value, "firing")
        self.assertIsNone(dto.assignee)
        self.assertEqual(dto.lifecycle["revision"], 0)

    def test_transition_initializes_state_for_pre_lifecycle_record(self):
        from keep.api.core import incident_lifecycle as life
        from keep.api.core.incident_configuration import configuration_scope
        from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            group = session.get(IncidentCorrelationGroup, row.rule_fingerprint)
            row.lifecycle_context, group.lifecycle_state = None, None
            session.add(row)
            session.add(group)
            session.commit()
        with configuration_scope("tenant"), Session(self.engine) as session:
            row, group = life.lock_incident(session, "tenant", identifier, self.actor())
            life.transition(session, row, "acknowledged", at=self.origin, actor=self.actor().email, group=group)
            session.commit()
            self.assertEqual(row.lifecycle_context["group_id"], group.id)
            self.assertEqual(row.lifecycle_context["policy_version"], group.rule_version)
            self.assertEqual(row.lifecycle_context["clock"], "receive_time")

    def test_alert_fingerprint_equal_to_foreign_incident_id_never_changes_that_incident(self):
        from keep.api.bl.enrichments_bl import EnrichmentsBl
        self.correlate(self.event(team="beta"))
        identifier = self.incidents()[0].id
        alert = self.save(self.event(str(identifier)), 1)
        with Session(self.engine) as session:
            EnrichmentsBl("tenant", db=session).enrich_entity(alert.fingerprint, {"status": "resolved"},
                ActionType.API_STATUS_CHANGE, self.actor().email, "Alert status update", should_exist=False)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(self.incidents()[0].team_id, "beta")
        self.assertEqual(self.audits(), [])
