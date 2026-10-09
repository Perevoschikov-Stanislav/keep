"""Signed actor commands, registered callback source and actual wire adapters."""

import copy
import json
import os
import requests
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from keep.api.models.db.incident_notification import IncidentIntegrationCommand, IncidentNotificationBinding
from keep.api.models.db.silence import NotificationDelivery
from tests.test_incident_notifications_fork import NotificationCase
from tests.incident_notification_fixtures import NotificationReceiver
from tests.silence_integration_fixtures import FakeReceiver, actor_token


class NotificationApiCase(NotificationCase):
    def setUp(self):
        super().setUp()
        from keep.api.core.silence_integrations import SilenceIntegrations
        from keep.api.models.db.incident_configuration import IncidentConfiguration
        from keep.identitymanager import silence_integration_auth
        self.receiver_a, self.receiver_b = FakeReceiver(), FakeReceiver()
        self.webhook, self.chat = NotificationReceiver(), NotificationReceiver()
        for server in (self.receiver_a, self.receiver_b, self.webhook, self.chat):
            self.addCleanup(server.close)
        self.service_keys = {"a": "local-client-alpha-fixture", "b": "local-client-beta-fixture"}
        self.enterContext(patch.dict(os.environ, {"NOTIFICATIONS_CLIENT_A": self.service_keys["a"],
            "NOTIFICATIONS_CLIENT_B": self.service_keys["b"]}))
        self.bundle["proof_profiles"] = [{"id": "proof-" + suffix, "issuer": server.url,
            "audience": "keep-actor", "jwks_url": server.url + "/jwks", "algorithms": [algorithm],
            "authorized_parties": ["interactive-" + suffix], "required_claims": {"typ": "Bearer", "user_kind": "human"},
            "clock_skew_seconds": 0} for suffix, server, algorithm in (
                ("a", self.receiver_a, "RS256"), ("b", self.receiver_b, "ES256"))]
        self.bundle["service_clients"] = [{"id": "client-" + suffix, "origin": "notification-test-" + suffix,
            "auth_ref": "env:NOTIFICATIONS_CLIENT_" + suffix.upper(), "team_ids": [team],
            "scopes": ["read:incident", "update:incident", "update:notification", "read:silence", "write:silence", "update:silence"],
            "proof_profile_refs": ["proof-" + suffix]} for suffix, team in (("a", "alpha"), ("b", "beta"))]
        self.bundle["transports"][0]["endpoint"] = self.webhook.url
        self.bundle["transports"][1].update(endpoint=self.chat.url, callback_client_ref="client-a")
        self.apply()
        def settings():
            with Session(self.engine) as session:
                return SilenceIntegrations.from_snapshot(session.get(IncidentConfiguration, "tenant").snapshot)
        self.settings = settings()
        self.enterContext(patch.object(silence_integration_auth, "get_silence_integrations", side_effect=settings))
        self.enterContext(patch.object(silence_integration_auth, "actor_proof_verifier", silence_integration_auth.ActorProofVerifier()))
        self.enterContext(patch("keep.api.routes.incident_notifications.utc_now", return_value=self.now(1)))
        self.enterContext(patch("keep.api.core.incident_notifications.utc_now", return_value=self.now(1)))
        from keep.api.routes import incident_notifications, silence_integrations, incidents
        from keep.api.core.db import get_session
        from keep.api.core.dependencies import get_pusher_client
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        app = FastAPI()
        app.include_router(incident_notifications.router, prefix="/integrations/notifications")
        app.include_router(silence_integrations.router, prefix="/integrations/silences")
        app.include_router(incidents.router, prefix="/incidents")
        self.ui_actor = AuthenticatedEntity(tenant_id="tenant", email="operator", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        route = next(route for route in app.routes if route.path == "/incidents/{incident_id}/commands")
        verifier = next(dependency.call for dependency in route.dependant.dependencies if dependency.name == "entity")
        app.dependency_overrides[verifier] = lambda: self.ui_actor
        app.dependency_overrides[get_pusher_client] = lambda: None
        def session():
            with Session(self.engine) as value:
                yield value
        app.dependency_overrides[get_session] = session
        self.client = self.enterContext(TestClient(app))

    def headers(self, client="a", *, proof=True, **claims):
        headers = {"X-API-KEY": self.service_keys[client]}
        if proof:
            headers["X-Keep-Actor-Token"] = actor_token(self, client=client, **claims)
        return headers

    def post(self, command=None, *, headers=None, **fields):
        body = json.loads((command or self.command(**fields)).json(exclude_unset=True))
        return self.client.post("/integrations/notifications/commands", json=body, headers=headers or self.headers())

    def wire_worker(self, seconds=0):
        worker = self.dispatcher(seconds)
        worker.sender = worker._send
        return worker

    def receipt_body(self):
        row = next(row for row in self.deliveries() if row.destination_id == "alpha-chat")
        return {"schema_version": 1, "notification_id": str(row.id), "destination_ref": row.destination_id,
            "status": "delivered", "external_id": next(iter(self.chat.posts)),
            "delivered_revision": row.payload["projection_revision"]}


class IncidentNotificationsApiTest(NotificationApiCase):
    def test_snapshot_page_size_uses_iac_default_and_ceiling(self):
        self.bundle.setdefault("dispatch", {})["snapshot_page_size"] = 1
        self.apply()
        self.correlate(self.event())
        self.correlate(self.event("second", workload="other"))
        headers = self.headers(proof=False)
        first = self.client.get("/integrations/notifications", headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(len(first.json()["items"]), 1)
        second = self.client.get("/integrations/notifications", params={"cursor": first.json()["next_cursor"]}, headers=headers)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertNotEqual(first.json()["items"][0]["incident"]["id"], second.json()["items"][0]["incident"]["id"])
        self.assertIsNone(second.json()["next_cursor"])
        too_large = self.client.get("/integrations/notifications", params={"limit": 2}, headers=headers)
        self.assertEqual(too_large.status_code, 422)
        self.bundle["dispatch"]["snapshot_page_size"] = 1000
        self.apply()
        large = self.client.get("/integrations/notifications", params={"limit": 1000}, headers=headers)
        self.assertEqual(large.status_code, 200, large.text)

    def test_refused_connection_retries_after_receiver_recovers(self):
        self.correlate(self.event())
        self.webhook.close()
        self.wire_worker().run_once()
        row = next(item for item in self.deliveries() if item.destination_id == "alpha-http")
        self.assertEqual((row.state, row.last_error_code), ("pending", "connect_unavailable"))
        self.assertFalse(row.effect_started)
        self.assertEqual(len(self.chat.posts), 1)
        self.webhook.restart()
        self.wire_worker(1).run_once()
        row = next(item for item in self.deliveries() if item.destination_id == "alpha-http")
        self.assertEqual((row.state, row.attempts), ("delivered", 2))
        self.assertEqual(len(self.webhook.events), 1)

    def test_connection_reset_does_not_allow_an_unsafe_retry(self):
        from urllib3.exceptions import ProtocolError
        self.correlate(self.event())
        error = requests.ConnectionError(ProtocolError("Connection aborted", ConnectionResetError("fixture reset")))
        with patch("keep.providers.http_provider.http_provider.requests.request", side_effect=error):
            self.wire_worker().run_once()
        self.wire_worker(1).run_once()
        self.assertTrue(all(row.state == "unknown" for row in self.deliveries()))
        self.assertEqual(self.webhook.events + self.chat.events, [])

    def test_missing_credential_is_retryable_without_an_external_effect(self):
        self.correlate(self.event())
        with patch.dict(os.environ, {"KEEP_NOTIFICATION_TEST_KEY": ""}):
            self.wire_worker().run_once()
        row = next(item for item in self.deliveries() if item.destination_id == "alpha-chat")
        self.assertEqual((row.state, row.last_error_code), ("pending", "credential_unavailable"))
        self.assertEqual(self.chat.events, [])
        self.wire_worker(1).run_once()
        self.assertEqual(len(self.chat.posts), 1)

    def test_iac_username_claim_maps_assignee_without_changing_actor_identity(self):
        self.bundle["proof_profiles"][0]["username_claim"] = "preferred_username"
        self.apply()
        self.correlate(self.event())
        response = self.post(self.command("assign", assignee="engineer"),
            headers=self.headers(sub="opaque-id", preferred_username="engineer", __keep_actor_username="forged-admin"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["assignee"], "engineer")
        missing = self.post(self.command("resolve"), headers=self.headers(sub="opaque-id"))
        self.assertEqual(missing.status_code, 401)

    def test_normal_keep_confirmation_shares_revision_and_scopes(self):
        self.correlate(self.event())
        command = self.command()
        path = "/incidents/" + str(command.incident_id) + "/commands"
        response = self.client.post(path, json=json.loads(command.json(exclude_unset=True)))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(self.client.post(path, json=json.loads(command.json(exclude_unset=True))).json()["replayed"], True)

    def test_legacy_api_key_cannot_supply_human_confirmation(self):
        self.correlate(self.event())
        self.ui_actor.api_key_name = "legacy-transport-key"
        self.ui_actor.role = "admin"
        command = self.command()
        response = self.client.post("/incidents/" + str(command.incident_id) + "/commands",
            json=json.loads(command.json(exclude_unset=True)))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_domain_audit_failure_rolls_back_status_receipt_and_queue(self):
        self.correlate(self.event())
        with patch("keep.api.core.db.add_audit", side_effect=RuntimeError("Storage unavailable")), self.assertRaises(RuntimeError):
            self.post()
        self.assertEqual(self.incidents()[0].status, "firing")
        self.assertEqual(self.deliveries(), [])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(IncidentIntegrationCommand)).all(), [])

    def test_signed_ack_replay_and_canonical_alert_effects(self):
        from keep.api.models.db.alert import AlertEnrichment
        self.correlate(self.event())
        command = self.command()
        first = self.post(command)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "acknowledged")
        self.assertEqual(self.post(command).json()["replayed"], True)
        with Session(self.engine) as session:
            receipt = session.exec(select(IncidentIntegrationCommand)).one()
            self.assertEqual(receipt.actor["subject"], "operator-a")
            self.assertEqual(receipt.actor["issuer"], self.receiver_a.url)
            self.assertEqual(receipt.origin, "notification-test-a")
            self.assertEqual(session.exec(select(AlertEnrichment)).one().enrichments["status"], "acknowledged")

    def test_no_proof_no_role_and_foreign_direct_id_are_denied(self):
        self.correlate(self.event())
        self.assertEqual(self.post(headers=self.headers(proof=False)).status_code, 401)
        self.assertEqual(self.post(headers=self.headers(role="viewer")).status_code, 403)
        self.assertEqual(self.post(headers=self.headers("b", role="admin")).status_code, 404)
        self.assertEqual(self.post(headers=self.headers(groups=["/unknown"])).status_code, 403)
        self.assertEqual(self.incidents()[0].status, "firing")

    def test_callback_and_body_cannot_impersonate_admin(self):
        self.correlate(self.event())
        headers = {**self.headers(), "X-KEEP-ROLE": "admin"}
        self.assertEqual(self.post(headers=headers).status_code, 403)
        body = json.loads(self.command().json())
        for name in ("role", "actor", "channel_id", "origin", "team_id"):
            response = self.client.post("/integrations/notifications/commands", json={**body, name: "admin"}, headers=self.headers())
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.post(headers=self.headers("b", proof=False)).status_code, 401)

    def test_two_issuers_with_same_subject_have_distinct_receipts(self):
        self.correlate(self.event())
        first = self.command()
        self.assertEqual(self.post(first, headers=self.headers(sub="shared-operator")).status_code, 200)
        self.bundle["service_clients"][1]["team_ids"] = ["alpha", "beta"]
        self.apply()
        second = self.command("resolve", client_request_id=str(first.client_request_id))
        response = self.post(second, headers=self.headers("b", team="alpha", sub="shared-operator"))
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(IncidentIntegrationCommand)).all()), 2)

    def test_snapshot_and_delivery_metadata_are_team_scoped(self):
        self.correlate(self.event())
        self.correlate(self.event("beta-event", team="beta"))
        self.dispatcher().run_once()
        for suffix, team in (("a", "alpha"), ("b", "beta")):
            response = self.client.get("/integrations/notifications", headers=self.headers(suffix, proof=False))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({item["incident"]["team_id"] for item in response.json()["items"]}, {team})
            deliveries = self.client.get("/integrations/notifications/deliveries", headers=self.headers(suffix, proof=False))
            self.assertEqual({item["team_id"] for item in deliveries.json()["items"]}, {team})

    def test_unassigned_snapshot_scope_does_not_grant_access_to_foreign_history(self):
        from keep.api.models.db.incident import Incident
        from keep.api.models.db.alert import LastAlertToIncident
        self.bundle["service_clients"][0]["team_ids"] = ["alpha", None]
        self.apply()
        self.correlate(self.event())
        with Session(self.engine) as session:
            clean = Incident(tenant_id="tenant", team_id=None, status="firing", severity=3, user_summary="Own unassigned data")
            session.add(clean)
            session.flush()
            mixed = Incident(tenant_id="tenant", team_id=None, status="firing", severity=3, user_summary="Foreign linked data")
            session.add(mixed)
            session.flush()
            session.add(LastAlertToIncident(tenant_id="tenant", incident_id=mixed.id, fingerprint="p-1"))
            session.commit()
            clean_id, mixed_id = str(clean.id), str(mixed.id)
        response = self.client.get("/integrations/notifications", headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200)
        identifiers = {item["incident"]["id"] for item in response.json()["items"]}
        self.assertIn(clean_id, identifiers)
        self.assertNotIn(mixed_id, identifiers)

    def test_actual_webhook_appends_and_api_updates_one_post(self):
        self.correlate(self.event())
        self.wire_worker().run_once()
        self.change("acknowledged", 1)
        self.wire_worker(1).run_once()
        self.assertEqual(len(self.chat.posts), 1)
        self.assertEqual([item["method"] for item in self.chat.events], ["POST", "PUT"])
        self.assertEqual(len(self.webhook.events), 2)
        dto = self.webhook.events[0]["body"]
        self.assertNotIn("channel_id", dto)
        self.assertTrue(dto["actions"][0]["keep_url"].endswith("?command=ack&revision=0"))
        self.assertEqual(self.chat.events[0]["authorization"], "Bearer local-fixture-only")
        self.assertNotIn("actions", self.chat.events[0]["body"]["props"]["attachments"][0])

    def test_duplicate_out_of_order_dto_and_snapshot_recovery_do_not_execute_commands(self):
        self.correlate(self.event())
        self.wire_worker().run_once()
        old = copy.deepcopy(self.webhook.events[0]["body"])
        self.change("acknowledged", 1)
        self.wire_worker(1).run_once()
        new = copy.deepcopy(self.webhook.events[-1]["body"])
        for body in (old, new, old):
            with requests.post(self.webhook.url + "/events", json=body, timeout=1) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.webhook.projection[new["incident_id"]]["projection_revision"], new["projection_revision"])
        before = len(self.deliveries())
        response = self.client.get("/integrations/notifications", headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["incident"]["status"], "acknowledged")
        echo = self.client.post("/integrations/notifications/commands", json=new, headers=self.headers())
        self.assertEqual(echo.status_code, 422)
        self.assertEqual(len(self.deliveries()), before)

    def test_unknown_create_recovery_verifies_external_post_before_update(self):
        self.correlate(self.event())
        self.chat.status = 503  # The fake accepts the create, then returns an ambiguous failure.
        self.wire_worker().run_once()
        self.assertEqual(sum(row.state == "unknown" for row in self.deliveries()), 1)
        body = self.receipt_body()
        response = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["state"], "delivered")
        replay = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers(proof=False))
        self.assertTrue(replay.json()["replayed"])
        self.chat.status = 200
        self.change("acknowledged", 2)
        self.wire_worker(2).run_once()
        self.assertEqual(len(self.chat.posts), 1)
        self.assertEqual(self.chat.events[-1]["method"], "PUT")

    def test_receipt_from_foreign_service_or_wrong_channel_cannot_release_hold(self):
        self.correlate(self.event())
        self.chat.status = 503
        self.wire_worker().run_once()
        body = self.receipt_body()
        foreign = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers("b", proof=False))
        self.assertEqual(foreign.status_code, 404)
        self.chat.tamper_receipt = True
        invalid = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers(proof=False))
        self.assertEqual(invalid.status_code, 409)
        with Session(self.engine) as session:
            self.assertTrue(session.exec(select(IncidentNotificationBinding).where(IncidentNotificationBinding.transport_id == "chat-api")).one().uncertain)

    def test_receipt_requires_the_transport_registered_source_even_in_same_team(self):
        self.bundle["service_clients"][1]["team_ids"] = ["alpha", "beta"]
        self.apply()
        self.correlate(self.event())
        self.chat.status = 503
        self.wire_worker().run_once()
        response = self.client.post("/integrations/notifications/receipts", json=self.receipt_body(),
            headers=self.headers("b", proof=False))
        self.assertEqual(response.status_code, 403)

    def test_old_receipt_replay_never_regresses_confirmed_projection(self):
        self.correlate(self.event())
        self.wire_worker().run_once()
        body = self.receipt_body()
        self.change("acknowledged", 2)
        self.wire_worker(2).run_once()
        response = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["replayed"])
        with Session(self.engine) as session:
            binding = session.exec(select(IncidentNotificationBinding).where(IncidentNotificationBinding.transport_id == "chat-api")).one()
            self.assertGreater(binding.confirmed_revision, body["delivered_revision"])

    def test_failure_receipt_cannot_prove_an_ambiguous_create_was_absent(self):
        self.correlate(self.event())
        self.chat.status = 503
        self.wire_worker().run_once()
        body = {**self.receipt_body(), "status": "failed", "external_id": None, "delivered_revision": None}
        response = self.client.post("/integrations/notifications/receipts", json=body, headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["state"], "unknown")

    def test_same_silence_api_accepts_proof_and_blocks_incident_delivery(self):
        from keep.api.models.silence import utc_string
        from keep.api.models.db.user import User
        self.correlate(self.event())
        with Session(self.engine) as session:
            session.add(User(tenant_id="tenant", username="operator-a", role="responder", password_hash="unused"))
            session.commit()
        body = {"schema_version": 1, "client_request_id": str(uuid4()), "team_id": "alpha",
            "selector": {"kind": "incident", "incident_ids": [str(self.incidents()[0].id)]},
            "starts_at": None, "ends_at": utc_string(self.now(20)), "comment": "From notification", "correlation_id": "external-message"}
        with patch("keep.api.bl.silences_bl.utc_now", return_value=self.now(1)):
            response = self.client.post("/integrations/silences", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)
        self.wire_worker(1).run_once()
        self.assertEqual(self.chat.events + self.webhook.events, [])
