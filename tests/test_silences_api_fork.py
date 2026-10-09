"""Silence v1: authorization, transaction boundaries and effective coverage."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from keep.api.bl.silences_bl import SilencesBL
from keep.api.bl.silences_evaluator import SilenceEvaluator
from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.silence import Silence, SilenceCommand, SilenceEvent
from keep.api.models.db.tenant import Tenant
from keep.api.models.db.user import User
from keep.api.models.silence import (
    CancelSilenceCommand, CreateSilenceCommand, EffectiveSilenceQuery,
    UpdateSilenceCommand, utc_string,
)
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory
from keep.identitymanager.team_policy import get_team_policy
from tests.team_fork_test_case import TeamDatabaseTestCase

NOW = datetime(2026, 10, 4, 12, 0, 0)


class SilenceDatabaseCase(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as session:
            session.add(Tenant(id="keep", name="Test"))
            session.add(Tenant(id="another", name="Other"))
            session.add(User(id=1, tenant_id="keep", username="member@example.test", password_hash="unused", role="responder"))
            session.add(User(id=2, tenant_id="keep", username="admin@example.test", password_hash="unused", role="admin"))
            session.commit()
        self.entity = AuthenticatedEntity(tenant_id="keep", email="member@example.test", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        self.admin = AuthenticatedEntity(tenant_id="keep", email="admin@example.test", role="admin")
        self.alpha_id = self.alert("a", "alpha")
        self.beta_id = self.alert("b", "beta")
        self.unassigned_id = self.alert("u", None)

    def alert(self, fingerprint, team_id, *, status="firing", tenant="keep", **fields):
        with Session(self.engine) as session:
            row = Alert(tenant_id=tenant, team_id=team_id, fingerprint=fingerprint,
                timestamp=NOW, provider_type="test", provider_id="test",
                event={"name": fingerprint, "fingerprint": fingerprint, "status": status,
                       "lastReceived": utc_string(NOW), "severity": "high", **fields})
            session.add(row)
            session.flush()
            last = session.get(LastAlert, (tenant, fingerprint))
            if last:
                last.alert_id = row.id
                last.timestamp = NOW
                session.add(last)
            else:
                session.add(LastAlert(tenant_id=tenant, fingerprint=fingerprint, alert_id=row.id,
                    timestamp=NOW, first_timestamp=NOW))
            session.commit()
            return row.id

    def incident(self, fingerprints, *, team="alpha", status="firing"):
        with Session(self.engine) as session:
            incident = Incident(tenant_id="keep", team_id=team, status=status, is_candidate=False,
                user_generated_name="Incident", alerts_count=len(fingerprints))
            session.add(incident)
            session.flush()
            for fingerprint in fingerprints:
                session.add(LastAlertToIncident(tenant_id="keep", fingerprint=fingerprint, incident_id=incident.id))
            session.commit()
            return incident.id

    def command(self, selector=None, **fields):
        return CreateSilenceCommand.parse_obj({
            "schema_version": 1, "client_request_id": str(uuid4()), "team_id": "alpha",
            "selector": selector or {"kind": "alert", "fingerprints": ["a"]},
            "starts_at": None, "ends_at": utc_string(NOW + timedelta(hours=1)),
            "comment": "Maintenance", "correlation_id": None, **fields,
        })

    def create(self, command=None, *, entity=None, now=NOW):
        with Session(self.engine) as session:
            return SilencesBL(session, entity or self.entity, now).create(command or self.command())[0]

    def effective(self, *targets, now=NOW, entity=None):
        with Session(self.engine) as session:
            query = EffectiveSilenceQuery(schema_version=1, targets=list(targets))
            return SilenceEvaluator(session, "keep", now).effective(entity or self.entity, query.targets)

    def counts(self):
        with Session(self.engine) as session:
            return tuple(len(session.exec(select(model)).all()) for model in (Silence, SilenceCommand, SilenceEvent))


class SilenceCommandsTest(SilenceDatabaseCase):
    def test_create_audit_and_permanent_replay_after_expiry(self):
        command = self.command()
        result = self.create(command)
        self.assertTrue(result.result.created_by.subject.startswith("keep-user:"))
        self.assertEqual(result.result.state, "active")
        replay = self.create(command, now=NOW + timedelta(days=2))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.result, result.result)
        self.assertEqual(self.counts(), (1, 1, 1))
        with Session(self.engine) as session:
            self.assertEqual(SilencesBL(session, self.entity, NOW + timedelta(days=2)).get(result.result.id).state, "expired")
            event_row = session.exec(select(SilenceEvent)).one()
            self.assertEqual(event_row.payload["resource"], json.loads(result.result.json()))
            self.assertEqual(event_row.payload["client_request_id"], str(command.client_request_id))

    def test_nonce_conflicts_and_operator_namespace(self):
        command = self.command()
        result = self.create(command)
        with self.assertRaises(HTTPException) as error:
            self.create(command.copy(update={"comment": "Different"}))
        self.assertEqual(error.exception.detail["code"], "idempotency_conflict")
        other = self.create(command, entity=self.admin)
        self.assertNotEqual(other.result.id, result.result.id)
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_recreated_account_with_reused_id_does_not_replay_other_operators_command(self):
        command = self.command()
        first = self.create(command)
        with Session(self.engine) as session:
            session.delete(session.get(User, 1))
            session.commit()
            session.add(User(id=1, tenant_id="keep", username="member@example.test", password_hash="unused",
                role="responder", created_at=NOW + timedelta(days=1)))
            session.commit()
        second = self.create(command)
        self.assertFalse(second.replayed)
        self.assertNotEqual(first.result.created_by.subject, second.result.created_by.subject)
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_revision_noop_update_cancel_and_retry(self):
        original = self.create().result
        update_command = UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
            changes={"ends_at": utc_string(NOW + timedelta(hours=3))}, correlation_id=None)
        with Session(self.engine) as session:
            updated, _ = SilencesBL(session, self.entity, NOW).update(original.id, update_command)
        self.assertEqual(updated.result.revision, 2)
        with Session(self.engine) as session:
            replay, _ = SilencesBL(session, self.entity, NOW + timedelta(days=1)).update(original.id, update_command)
        self.assertTrue(replay.replayed)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
            SilencesBL(session, self.entity, NOW).cancel(original.id, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1, reason="End", correlation_id=None))
        self.assertEqual(error.exception.detail["current_revision"], 2)
        cancel = CancelSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=2, reason="End", correlation_id="incident")
        with Session(self.engine) as session:
            cancelled, _ = SilencesBL(session, self.entity, NOW).cancel(original.id, cancel)
        self.assertEqual((cancelled.result.state, cancelled.result.revision), ("cancelled", 3))
        with Session(self.engine) as session:
            noop, _ = SilencesBL(session, self.entity, NOW).cancel(original.id, cancel.copy(update={"client_request_id": uuid4(), "expected_revision": 3}))
        self.assertEqual(noop.result.revision, 3)
        self.assertEqual(self.counts(), (1, 4, 3))
        with Session(self.engine) as session:
            retry, _ = SilencesBL(session, self.entity, NOW + timedelta(days=2)).cancel(original.id, cancel)
        self.assertTrue(retry.replayed)

    def test_group_validation_and_audit_failure_are_atomic(self):
        with self.assertRaises(HTTPException) as error:
            self.create(self.command({"kind": "alert", "fingerprints": ["a", "b"]}))
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0))
        def reject_audit(mapper, connection, target):
            raise RuntimeError("Audit write failed")
        event.listen(SilenceEvent, "before_insert", reject_audit)
        try:
            with self.assertRaisesRegex(RuntimeError, "Audit write"):
                self.create()
        finally:
            event.remove(SilenceEvent, "before_insert", reject_audit)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_shared_visibility_does_not_grant_writes(self):
        document = yaml.safe_load(os.environ["KEEP_TEAMS_CONFIG"])
        document["visibility"] = "all"
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(document)
        get_team_policy.cache_clear()
        foreign = self.create(self.command(team_id="beta", selector={"kind": "alert", "fingerprints": ["b"]}), entity=self.admin).result
        with Session(self.engine) as session:
            self.assertEqual(SilencesBL(session, self.entity, NOW).get(foreign.id).id, foreign.id)
            with self.assertRaises(HTTPException) as error:
                SilencesBL(session, self.entity, NOW).cancel(foreign.id, CancelSilenceCommand(schema_version=1,
                    client_request_id=uuid4(), expected_revision=1, reason="End", correlation_id=None))
        self.assertEqual(error.exception.status_code, 403)

    def test_mixed_history_unassigned_and_single_team_scope(self):
        self.alert("a", "beta")
        with self.assertRaises(HTTPException) as error:
            self.create()
        self.assertEqual(error.exception.status_code, 404)
        result = self.create(entity=self.admin)
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}, entity=self.admin).items[0].silenced)
        null_rule = self.create(self.command(team_id=None, selector={"kind": "filter", "cel": "true"}), entity=self.admin)
        self.assertIsNone(null_rule.result.team_id)
        results = self.effective({"kind": "alert", "fingerprint": "u"}, {"kind": "alert", "fingerprint": "b"}, entity=self.admin).items
        self.assertEqual([item.silenced for item in results], [True, False])
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            SilencesBL(session, self.entity, NOW).get(result.result.id)

    def test_time_validation_and_scheduled_replanning(self):
        for fields in ({"starts_at": utc_string(NOW - timedelta(seconds=1))}, {"ends_at": utc_string(NOW)}, {"team_id": "unknown"}):
            with self.subTest(fields=fields), self.assertRaises(HTTPException) as error:
                self.create(self.command(**fields), entity=self.admin)
            self.assertEqual(error.exception.status_code, 422)
        scheduled = self.create(self.command(starts_at=utc_string(NOW + timedelta(minutes=10)))).result
        self.assertEqual(scheduled.state, "scheduled")
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        with Session(self.engine) as session:
            update_command = UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes={"starts_at": utc_string(NOW)}, correlation_id=None)
            result, _ = SilencesBL(session, self.entity, NOW).update(scheduled.id, update_command)
        self.assertEqual(result.result.state, "active")
        self.assertTrue(self.effective({"kind": "alert", "fingerprint": "a"}, now=NOW + timedelta(minutes=59)).items[0].silenced)
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}, now=NOW + timedelta(hours=1)).items[0].silenced)

    def test_noop_active_start_and_expired_cannot_be_revived(self):
        rule = self.create().result
        def command(changes):
            return UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
                changes=changes, correlation_id=None)
        with Session(self.engine) as session:
            noop, _ = SilencesBL(session, self.entity, NOW).update(rule.id, command({"comment": rule.comment}))
        self.assertEqual(noop.result.revision, 1)
        self.assertEqual(self.counts(), (1, 2, 1))
        with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
            SilencesBL(session, self.entity, NOW).update(rule.id, command({"starts_at": utc_string(NOW + timedelta(minutes=1))}))
        self.assertEqual(error.exception.detail["code"], "invalid_time")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as error:
            SilencesBL(session, self.entity, NOW + timedelta(days=1)).update(rule.id, command({"ends_at": None}))
        self.assertEqual(error.exception.detail["code"], "invalid_state")

    def test_membership_in_both_teams_does_not_allow_mixed_fingerprint_write(self):
        self.alert("a", "beta")
        entity = AuthenticatedEntity(tenant_id="keep", email="member@example.test", role="responder",
            teams=frozenset({"alpha", "beta"}), visible_teams=frozenset({"alpha", "beta"}))
        with self.assertRaises(HTTPException) as error:
            self.create(entity=entity)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_owner_change_does_not_expand_rule_and_admin_can_cancel_it(self):
        original = self.create().result
        with Session(self.engine) as session:
            alert = session.get(Alert, self.alpha_id)
            alert.team_id = "beta"
            session.add(alert)
            session.commit()
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}, entity=self.admin).items[0].silenced)
        with Session(self.engine) as session:
            self.assertEqual(SilencesBL(session, self.admin, NOW).get(original.id).team_id, "alpha")
            cancelled, _ = SilencesBL(session, self.admin, NOW).cancel(original.id, CancelSilenceCommand(
                schema_version=1, client_request_id=uuid4(), expected_revision=1, reason="Owner changed", correlation_id=None))
        self.assertEqual(cancelled.result.state, "cancelled")


class SilenceCoverageTest(SilenceDatabaseCase):
    def test_repeat_resolve_overlap_and_independent_cancel(self):
        first = self.create().result
        self.create(self.command(ends_at=None))
        self.alert("a", "alpha", status="resolved")
        item = self.effective({"kind": "alert", "fingerprint": "a"}).items[0]
        self.assertTrue(item.silenced)
        self.assertIsNone(item.silenced_until)
        self.assertEqual(len(item.reasons), 2)
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, NOW).cancel(first.id, CancelSilenceCommand(schema_version=1,
                client_request_id=uuid4(), expected_revision=1, reason="End", correlation_id=None))
        self.assertTrue(self.effective({"kind": "alert", "fingerprint": "a"}, now=NOW + timedelta(days=2)).items[0].silenced)

    def test_filter_severity_missing_nonboolean_and_self_reference(self):
        for cel in ("missing == 'value'", "42", "dismissed == true", "silence.silenced == true"):
            self.create(self.command({"kind": "filter", "cel": cel}))
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        self.create(self.command({"kind": "filter", "cel": "severity >= 'warning' && status == 'firing'"}))
        self.assertTrue(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        self.alert("a", "alpha", status="resolved")
        self.assertFalse(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        with self.assertRaises(HTTPException) as error:
            self.create(self.command({"kind": "filter", "cel": "status =="}))
        self.assertEqual(error.exception.detail["code"], "invalid_selector")

    def test_legacy_flags_do_not_control_cel_and_invalid_stored_rule_fails_closed(self):
        self.alert("a", "alpha", dismissed=True, dismissUntil="invalid legacy time")
        result = self.create(self.command({"kind": "filter", "cel": "status == 'firing'"})).result
        self.assertTrue(self.effective({"kind": "alert", "fingerprint": "a"}).items[0].silenced)
        with Session(self.engine) as session:
            row = session.get(Silence, result.id)
            row.selector = {"kind": "filter", "cel": "status =="}
            session.add(row)
            session.commit()
        with self.assertRaises(HTTPException) as error:
            self.effective({"kind": "alert", "fingerprint": "a"})
        self.assertEqual(error.exception.status_code, 503)

    def test_incident_partial_full_deadline_and_resolved(self):
        self.alert("a2", "alpha")
        incident_id = self.incident(["a", "a2"])
        target = {"kind": "incident", "incident_id": str(incident_id)}
        self.create()
        partial = self.effective(target).items[0]
        self.assertEqual((partial.silenced, partial.coverage, partial.total_alerts, partial.silenced_alerts), (False, "partial", 2, 1))
        self.create(self.command({"kind": "alert", "fingerprints": ["a2"]}, ends_at=utc_string(NOW + timedelta(hours=2))))
        full = self.effective(target).items[0]
        self.assertEqual(full.silenced_until, utc_string(NOW + timedelta(hours=1)))
        self.alert("a", "alpha", status="resolved")
        self.alert("a2", "alpha", status="resolved")
        self.assertEqual(self.effective(target).items[0].coverage, "none")
        with Session(self.engine) as session:
            row = session.get(Incident, incident_id)
            row.status = "resolved"
            session.add(row)
            session.commit()
        self.assertTrue(self.effective(target).items[0].silenced)

    def test_shared_alert_inheritance_does_not_recurse(self):
        self.alert("a2", "alpha")
        first = self.incident(["a"])
        second = self.incident(["a", "a2"])
        empty = self.incident([])
        self.create(self.command({"kind": "incident", "incident_ids": [str(first), str(empty)]}))
        result = self.effective({"kind": "incident", "incident_id": str(second)},
            {"kind": "alert", "fingerprint": "a2"}, {"kind": "incident", "incident_id": str(empty)}).items
        self.assertEqual([(item.silenced, item.coverage) for item in result], [(False, "partial"), (False, "none"), (True, "full")])

    def test_incident_coverage_is_not_limited_to_500(self):
        fingerprints = ["a"] + [f"many-{i}" for i in range(501)]
        with Session(self.engine) as session:
            for fingerprint in fingerprints[1:]:
                row = Alert(tenant_id="keep", team_id="alpha", fingerprint=fingerprint, timestamp=NOW,
                    provider_type="test", provider_id="test", event={"name": fingerprint, "status": "firing", "severity": "high", "lastReceived": utc_string(NOW)})
                session.add(row)
                session.flush()
                session.add(LastAlert(tenant_id="keep", fingerprint=fingerprint, alert_id=row.id, timestamp=NOW, first_timestamp=NOW))
            session.commit()
        incident = self.incident(fingerprints)
        self.create(self.command({"kind": "alert", "fingerprints": fingerprints[:-1]}))
        item = self.effective({"kind": "incident", "incident_id": str(incident)}).items[0]
        self.assertEqual((item.coverage, item.total_alerts, item.silenced_alerts), ("partial", 502, 501))


class SilenceHttpTest(SilenceDatabaseCase):
    def setUp(self):
        super().setUp()
        from keep.api.routes import silences

        self.routes = silences
        self.enterContext(patch("keep.api.bl.silences_bl.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))
        app = FastAPI()
        app.include_router(silences.router, prefix="/silences")
        self.verifiers = {}
        for dependency in (silences.read_identity, silences.create_identity, silences.update_identity):
            verifier = IdentityManagerFactory.get_auth_verifier(dependency.scopes)
            self.verifiers[dependency] = verifier
            app.dependency_overrides[dependency] = verifier
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def headers(self, role="responder", team="alpha"):
        return {"x-forwarded-email": "member@example.test", "x-forwarded-groups": f"/roles/{role}, /teams/{team}"}

    def body(self, **fields):
        return json.loads(self.command(**fields).json())

    def test_alertmanager_mirrors_are_readable_and_source_owned(self):
        from keep.api.core.alertmanager_reconciliation import AlertmanagerReconciler
        remote = {"id": str(uuid4()), "status": {"state": "active"}, "createdBy": "operator", "comment": "external rule",
                  "startsAt": "2026-10-04T11:00:00Z", "endsAt": "2026-10-04T13:00:00Z",
                  "matchers": [{"name": "alertname", "value": "a", "isRegex": False, "isEqual": True}]}
        with Session(self.engine) as session:
            AlertmanagerReconciler("http://fake-am", tenant_id="keep", clock=lambda: NOW).reconcile_silences(session, [remote])
        listing = self.client.get("/silences", headers=self.headers())
        self.assertEqual(listing.status_code, 200, listing.text)
        rows = listing.json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["read_only"])
        self.assertEqual(rows[0]["created_by"]["kind"], "service")
        identifier = rows[0]["id"]
        detail = self.client.get("/silences/" + identifier, headers=self.headers())
        self.assertEqual(detail.status_code, 200, detail.text)
        response = self.client.post("/silences/" + identifier + "/cancel", headers=self.headers(), json={
            "schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": rows[0]["revision"],
            "reason": "local cancellation", "correlation_id": None})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "externally_managed_silence")

    def test_wire_identity_validation_and_legacy_rejection(self):
        good = self.client.post("/silences", json=self.body(), headers=self.headers())
        self.assertEqual(good.status_code, 201, good.text)
        self.assertEqual(good.headers["Location"], "/silences/" + good.json()["result"]["id"])
        for extra in ({"actor": {}}, {"origin": "fake"}, {"tenant_id": "another"}, {"schema_version": True}):
            response = self.client.post("/silences", json={**self.body(), **extra}, headers=self.headers())
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["schema_version"], 1)
        with patch.object(self.verifiers[self.routes.create_identity], "authenticate") as verifier:
            response = self.client.post("/silences", json=self.body(), headers={**self.headers(), "X-KEEP-USER": "forged"})
            self.assertEqual(response.status_code, 403)
            verifier.assert_not_called()
        for role in ("viewer", "noc"):
            response = self.client.post("/silences", json=self.body(), headers=self.headers(role))
            self.assertEqual(response.status_code, 403)

    def test_server_visibility_effective_atomicity_and_cursor(self):
        for _ in range(3):
            self.create()
        foreign = self.create(self.command(team_id="beta", selector={"kind": "alert", "fingerprints": ["b"]}), entity=self.admin).result
        page = self.client.get("/silences?limit=2", headers=self.headers("viewer")).json()
        self.assertEqual(len(page["items"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        next_page = self.client.get("/silences", params={"limit": 2, "cursor": page["next_cursor"]}, headers=self.headers("viewer")).json()
        self.assertEqual(len(next_page["items"]), 1)
        self.assertIsNone(next_page["next_cursor"])
        changed = self.client.get("/silences", params={"state": "active", "cursor": page["next_cursor"]}, headers=self.headers("viewer"))
        self.assertEqual(changed.status_code, 422)
        hidden = self.client.get(f"/silences/{foreign.id}", headers=self.headers())
        self.assertEqual(hidden.status_code, 404)
        self.assertNotIn(str(foreign.id), hidden.text)
        batch = self.client.post("/silences/effective", headers=self.headers(), json={"schema_version": 1,
            "targets": [{"kind": "alert", "fingerprint": "a"}, {"kind": "alert", "fingerprint": "b"}]})
        self.assertEqual(batch.status_code, 404)
        self.assertNotIn("items", batch.json())

    def test_read_only_storage_failure_and_service_actor(self):
        with patch.dict(os.environ, {"KEEP_READ_ONLY": "true"}):
            response = self.client.post("/silences", json=self.body(), headers=self.headers())
        self.assertEqual(response.status_code, 403)
        service = AuthenticatedEntity(tenant_id="keep", email="service", role="admin", api_key_name="stable-reference")
        result = self.create(entity=service).result
        self.assertEqual((result.created_by.kind, result.created_by.subject), ("service", "stable-reference"))
        with patch("keep.api.routes.silences.SilenceEvaluator", side_effect=__import__("sqlalchemy").exc.OperationalError("hidden SQL", {}, Exception())):
            response = self.client.post("/silences/effective", headers=self.headers(), json={"schema_version": 1,
                "targets": [{"kind": "alert", "fingerprint": "a"}]})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hidden SQL", response.text)

    def test_actual_wire_responses_and_audit_samples(self):
        samples = []
        def capture(response, schema, status):
            self.assertEqual(response.status_code, status, response.text)
            samples.append({"schema": schema, "body": response.json()})
            return response.json()
        body = self.body()
        created = capture(self.client.post("/silences", json=body, headers=self.headers()), "MutationResponse", 201)
        silence_id = created["result"]["id"]
        capture(self.client.post("/silences", json=body, headers=self.headers()), "MutationResponse", 201)
        capture(self.client.get(f"/silences/{silence_id}", headers=self.headers()), "Silence", 200)
        capture(self.client.get("/silences?state=active", headers=self.headers()), "SilenceList", 200)
        capture(self.client.get("/silences", headers={}), "Error", 401)
        capture(self.client.get(f"/silences/{uuid4()}", headers=self.headers()), "Error", 404)
        capture(self.client.post("/silences", json={**body, "actor": {}}, headers=self.headers()), "Error", 422)
        capture(self.client.post("/silences", json={**body, "comment": "reuse"}, headers=self.headers()), "Error", 409)
        capture(self.client.post("/silences", json=body, headers=self.headers("viewer")), "Error", 403)
        incident_id = self.incident(["a"])
        capture(self.client.post("/silences/effective", headers=self.headers(), json={"schema_version": 1,
            "targets": [{"kind": "alert", "fingerprint": "a"}, {"kind": "incident", "incident_id": str(incident_id)}]}), "EffectiveResponse", 200)
        update_command = {"schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": 1,
                          "changes": {"comment": "Changed"}, "correlation_id": None}
        capture(self.client.patch(f"/silences/{silence_id}", json=update_command, headers=self.headers()), "MutationResponse", 200)
        cancel_command = {"schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": 2,
                          "reason": "End", "correlation_id": None}
        capture(self.client.post(f"/silences/{silence_id}/cancel", json=cancel_command, headers=self.headers()), "MutationResponse", 200)
        with Session(self.engine) as session:
            samples.extend({"schema": "LifecycleEvent", "body": row.payload} for row in session.exec(select(SilenceEvent)).all())
        destination = os.environ.get("KEEP_SILENCES_WIRE_SAMPLES")
        if destination:
            Path(destination).write_text(json.dumps(samples, indent=2) + "\n")

    def test_limits_strict_input_and_permissions_for_ui(self):
        invalid = [
            {"selector": {"kind": "unknown", "fingerprints": ["a"]}},
            {"selector": {"kind": "alert", "fingerprints": ["a", "a"]}},
            {"selector": {"kind": "alert", "fingerprints": [str(i) for i in range(1001)]}},
            {"selector": {"kind": "filter", "cel": "x" * 8193}},
            {"ends_at": "2026-10-04T15:00:00+03:00"},
            {"starts_at": "2026-02-30T12:00:00Z"},
            {"schema_version": "1"},
        ]
        for changes in invalid:
            with self.subTest(changes=next(iter(changes))):
                response = self.client.post("/silences", json={**self.body(), **changes}, headers=self.headers())
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.counts(), (0, 0, 0))
        from keep.api.routes.auth.users import get_my_permissions

        permissions = get_my_permissions(self.entity)
        self.assertTrue({"write:silence", "update:silence"} <= set(permissions["scopes"]))
        self.assertEqual(permissions["writable_teams"], ["alpha"])
