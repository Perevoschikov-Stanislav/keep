"""Real JWT signatures, canonical authorization and delegated command replay."""

import copy
import base64
import hashlib
import json
import os
import time
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from keep.api.core.silence_integrations import IntegrationConfigurationError, SilenceIntegrations, get_silence_integrations
from keep.api.models.db.silence import NotificationDelivery, Silence, SilenceEvent
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.tenant import TenantApiKey
from keep.api.models.silence import utc_string
from keep.identitymanager.team_policy import get_team_policy
from tests.silence_integration_fixtures import actor_token, configure_integrations, signing_keys
from tests.test_silences_api_fork import NOW, SilenceDatabaseCase


class SilenceIntegrationApiTest(SilenceDatabaseCase):
    def setUp(self):
        super().setUp()
        configure_integrations(self)
        from keep.api.routes import silence_integrations, silences
        from keep.api.core.db import get_session
        self.routes = silence_integrations
        app = FastAPI()
        app.include_router(silence_integrations.router, prefix="/integrations/silences")
        app.include_router(silences.router, prefix="/silences")
        def session():
            with Session(self.engine) as value:
                yield value
        app.dependency_overrides[get_session] = session
        for verifier in (silences.read_identity, silences.create_identity, silences.update_identity):
            app.dependency_overrides[verifier] = lambda: self.entity
        self.enterContext(patch("keep.api.bl.silences_bl.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))
        self.client = self.enterContext(TestClient(app))

    def headers(self, client="a", *, proof=True, **claims):
        result = {"X-API-KEY": self.service_keys[client]}
        if proof:
            result["X-Keep-Actor-Token"] = actor_token(self, client=client, **claims)
        return result

    def post(self, command=None, *, headers=None, **fields):
        command = command or self.command(**fields)
        return self.client.post("/integrations/silences", json=json.loads(command.json()),
                                headers=headers or self.headers())

    def test_verified_sub_and_registered_origin_replay_survive_token_refresh(self):
        command = self.command(correlation_id="external-command-123")
        first = self.post(command)
        self.assertEqual(first.status_code, 201)
        dto = first.json()["result"]
        self.assertEqual(dto["created_by"]["subject"], "operator-a")
        self.assertEqual(dto["created_by"]["issuer"], self.receiver_a.url)
        self.assertEqual(dto["origin"], "integration-one")
        replay = self.post(command, headers=self.headers(exp=int(time.time()) + 180))
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["result"], dto)
        self.assertEqual(self.counts(), (1, 1, 1))
        with Session(self.engine) as session:
            rows = session.exec(select(NotificationDelivery)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.payload["event_id"] for row in rows},
                             {str(session.exec(select(SilenceEvent)).one().event_id)})
            self.assertTrue(all("X-Keep-Actor-Token" not in json.dumps(row.payload) for row in rows))

    def test_nonce_conflict_and_two_independent_registered_clients(self):
        command = self.command()
        first = self.post(command)
        self.assertEqual(first.status_code, 201)
        conflict = self.post(command.copy(update={"comment": "Changed"}))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"], "idempotency_conflict")
        second = self.post(self.command(team_id="beta", selector={"kind": "alert", "fingerprints": ["b"]}),
                           headers=self.headers("b"))
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["result"]["origin"], "integration-two")
        self.assertEqual(second.json()["result"]["created_by"]["subject"], "operator-b")
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_missing_service_key_and_missing_proof_are_rejected(self):
        self.assertEqual(self.post(headers={"X-API-KEY": "not-a-service-key"}).status_code, 401)
        self.assertEqual(self.post(headers={"X-Keep-Actor-Token": actor_token(self)}).status_code, 401)
        missing = self.post(headers=self.headers(proof=False))
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["detail"]["code"], "actor_proof_required")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_invalid_signed_token_claims_never_create_a_rule(self):
        now = int(time.time())
        cases = {"issuer": {"iss": "https://untrusted.invalid"}, "audience": {"aud": "other"},
            "mixed_audience": {"aud": ["keep-actor", "other"]}, "party": {"azp": "backend-service"},
            "profile": {"typ": "ID"}, "user_profile": {"user_kind": "service"},
            "expired": {"iat": now - 30, "exp": now - 1}, "old": {"iat": now - 301, "exp": now + 120},
            "future_iat": {"iat": now + 20, "exp": now + 120}, "nbf": {"nbf": now + 20},
            "boolean_iat": {"iat": True}, "subject": {"sub": ""}, "groups_string": {"groups": "/roles/admin"},
            "short_groups": {"groups": ["admin"]}, "client_grant": {"gty": "client-credentials"},
            "client_subject": {"preferred_username": "service-account-interactive-a"}}
        for name, claims in cases.items():
            with self.subTest(case=name):
                response = self.post(headers=self.headers(**claims))
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"]["code"], "invalid_actor_proof")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_none_hmac_wrong_signature_and_other_clients_profile_are_rejected(self):
        good = actor_token(self)
        payload = jwt.decode(good, options={"verify_signature": False})
        invalid = [jwt.encode(payload, key=None, algorithm="none", headers={"kid": "rsa"}),
            jwt.encode(payload, key="synthetic-test-key-not-a-credential", algorithm="HS256", headers={"kid": "rsa"}),
            good[:-8] + "AAAAAAAA", actor_token(self, client="b")]
        for token in invalid:
            with self.subTest(variant=invalid.index(token)):
                self.assertEqual(self.post(headers={"X-API-KEY": self.service_keys["a"],
                                                   "X-Keep-Actor-Token": token}).status_code, 401)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_missing_required_claim_and_ambiguous_jwks_key_are_rejected(self):
        payload = jwt.decode(actor_token(self), options={"verify_signature": False})
        for claim in ("iat", "exp", "sub", "user_kind"):
            current = {key: value for key, value in payload.items() if key != claim}
            token = jwt.encode(current, signing_keys()[0]["rsa"], algorithm="RS256", headers={"kid": "rsa"})
            self.assertEqual(self.post(headers={"X-API-KEY": self.service_keys["a"],
                                              "X-Keep-Actor-Token": token}).status_code, 401)
        from keep.identitymanager.silence_integration_auth import actor_proof_verifier
        actor_proof_verifier.cache.clear()
        self.receiver_a.public_keys = self.receiver_a.public_keys + [self.receiver_a.public_keys[0]]
        self.assertEqual(self.post().status_code, 401)

    def test_jwks_unavailable_returns_503_without_admin_fallback(self):
        self.receiver_a.jwks_status = 503
        response = self.post(headers=self.headers(role="admin"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "actor_verification_unavailable")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_viewer_unknown_operator_and_foreign_membership_are_denied(self):
        self.assertEqual(self.post(headers=self.headers(role="viewer")).status_code, 403)
        self.assertEqual(self.post(headers=self.headers(groups=["/some/unrelated/group"])).status_code, 403)
        self.assertEqual(self.post(headers=self.headers(team="beta")).status_code, 404)
        self.assertEqual(self.post(self.command(team_id="beta", selector={"kind": "alert", "fingerprints": ["b"]}),
                                  headers=self.headers(role="admin")).status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_admin_still_cannot_exceed_service_teams_or_write_unassigned_without_scope(self):
        self.assertEqual(self.post(headers=self.headers(role="admin")).status_code, 201)
        self.assertEqual(self.post(self.command(team_id=None, selector={"kind": "alert", "fingerprints": ["u"]}),
                                  headers=self.headers(role="admin")).status_code, 404)
        result = self.post(self.command(team_id=None, selector={"kind": "alert", "fingerprints": ["u"]}),
                           headers=self.headers("b", role="admin"))
        self.assertEqual(result.status_code, 201)
        listed = self.client.get("/integrations/silences", headers=self.headers("b", proof=False))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["team_id"] for item in listed.json()["items"]], [None])

    def test_all_incident_and_fingerprint_targets_require_canonical_access(self):
        alpha = self.incident(["a"])
        beta = self.incident(["b"], team="beta")
        mixed = self.incident(["a", "b"])
        selectors = [{"kind": "alert", "fingerprints": ["a", "b"]},
                     {"kind": "incident", "incident_ids": [str(alpha), str(beta)]},
                     {"kind": "incident", "incident_ids": [str(mixed)]}]
        for selector in selectors:
            self.assertEqual(self.post(self.command(selector=selector), headers=self.headers(role="admin")).status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_legacy_impersonation_and_json_actor_origin_are_rejected(self):
        for header in ("X-KEEP-USER", "X-KEEP-ROLE", "X-Custom-Role"):
            with patch.dict(os.environ, {"KEEP_IMPERSONATION_ROLE_HEADER": "X-Custom-Role"}):
                response = self.post(headers={**self.headers(), header: "admin"})
                self.assertEqual(response.status_code, 403)
        body = json.loads(self.command().json())
        for field in ("actor", "origin", "role", "groups", "tenant_id", "token"):
            response = self.client.post("/integrations/silences", json={**body, field: "admin"}, headers=self.headers())
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_read_only_and_service_scope_ceiling_deny_commands(self):
        with patch.dict(os.environ, {"KEEP_READ_ONLY": "true"}):
            self.assertEqual(self.post(headers=self.headers(role="admin")).status_code, 403)
        self.settings.clients["client-a"]["scopes"] = ["read:silence"]
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_service_reads_recover_snapshot_and_effective_without_operator_proof(self):
        created = self.post().json()["result"]
        headers = self.headers(proof=False)
        response = self.client.get("/integrations/silences/" + created["id"], headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 1)
        coverage = self.client.post("/integrations/silences/effective", headers=headers,
            json={"schema_version": 1, "targets": [{"kind": "alert", "fingerprint": "a"}]})
        self.assertEqual(coverage.status_code, 200)
        self.assertTrue(coverage.json()["items"][0]["silenced"])
        forbidden = self.client.get("/integrations/silences/" + created["id"], headers=self.headers("b", proof=False))
        self.assertEqual(forbidden.status_code, 404)
        mixed = self.client.post("/integrations/silences/effective", headers=headers, json={"schema_version": 1,
            "targets": [{"kind": "alert", "fingerprint": "a"}, {"kind": "alert", "fingerprint": "b"}]})
        self.assertEqual(mixed.status_code, 404)
        self.assertNotIn("items", mixed.json())

    def test_extend_cancel_revision_and_lost_response_replay(self):
        original = self.post().json()["result"]
        path = "/integrations/silences/" + original["id"]
        update = {"schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": 1,
            "changes": {"ends_at": utc_string(NOW + timedelta(hours=2))}, "correlation_id": "external-extend"}
        result = self.client.patch(path, headers=self.headers(), json=update)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["result"]["revision"], 2)
        replay = self.client.patch(path, headers=self.headers(), json=update)
        self.assertTrue(replay.json()["replayed"])
        cancellation = {"schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": 1,
                        "reason": "Completed", "correlation_id": "external-cancel"}
        self.assertEqual(self.client.post(path + "/cancel", headers=self.headers(), json=cancellation).status_code, 409)
        cancellation["expected_revision"] = 2
        cancelled = self.client.post(path + "/cancel", headers=self.headers(), json=cancellation)
        self.assertEqual(cancelled.status_code, 200)
        replay = self.client.post(path + "/cancel", headers=self.headers(), json=cancellation)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.counts(), (1, 3, 3))
        diagnostics = self.client.get("/integrations/silences/deliveries", headers=self.headers(proof=False))
        self.assertEqual(diagnostics.status_code, 200)
        self.assertEqual(len(diagnostics.json()["items"]), 6)
        self.assertNotIn("payload", diagnostics.json()["items"][0])

    def test_ui_commands_use_the_same_outbox_without_mattermost_configuration(self):
        response = self.client.post("/silences", json=json.loads(self.command().json()))
        self.assertEqual(response.status_code, 201)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(NotificationDelivery)).all()), 2)
        self.assertTrue(all(transport["kind"] == "http_json" for transport in self.settings.transports.values()))

    def test_lifecycle_event_cannot_echo_as_a_command(self):
        self.post()
        with Session(self.engine) as session:
            payload = session.exec(select(SilenceEvent)).one().payload
        response = self.client.post("/integrations/silences", headers=self.headers(), json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_removed_target_keeps_authorized_replay_read_and_cancellation_available(self):
        command = self.command()
        original = self.post(command).json()["result"]
        with Session(self.engine) as session:
            session.delete(session.get(LastAlert, ("keep", "a")))
            session.delete(session.get(Alert, self.alpha_id))
            session.commit()
        replay = self.post(command)
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()["replayed"])
        path = "/integrations/silences/" + original["id"]
        self.assertEqual(self.client.get(path, headers=self.headers(proof=False)).status_code, 200)
        cancelled = self.client.post(path + "/cancel", headers=self.headers(), json={"schema_version": 1,
            "client_request_id": str(uuid4()), "expected_revision": 1, "reason": "Target removed", "correlation_id": None})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["result"]["state"], "cancelled")

    def test_hidden_existing_target_cannot_be_treated_as_a_removed_target(self):
        command = self.command()
        original = self.post(command).json()["result"]
        with Session(self.engine) as session:
            target = session.get(Alert, self.alpha_id)
            target.team_id = "beta"
            session.add(target)
            session.commit()
        self.assertEqual(self.post(command, headers=self.headers(role="admin")).status_code, 404)
        path = "/integrations/silences/" + original["id"]
        self.assertEqual(self.client.get(path, headers=self.headers(proof=False)).status_code, 404)

    def test_registered_client_cannot_bypass_proof_via_an_existing_admin_api_key(self):
        from keep.api.routes import silences
        with Session(self.engine) as session:
            session.add(TenantApiKey(tenant_id="keep", reference_id="legacy-admin-key",
                key_hash=hashlib.sha256(self.service_keys["a"].encode()).hexdigest(),
                created_by="admin@example.test", role="admin"))
            session.commit()
        for verifier in (silences.read_identity, silences.create_identity, silences.update_identity):
            self.client.app.dependency_overrides.pop(verifier)
        basic = base64.b64encode(("bridge:" + self.service_keys["a"]).encode()).decode()
        credentials = [{"X-API-KEY": self.service_keys["a"]}, {"Authorization": "Basic " + basic},
                       {"Authorization": "Digest " + self.service_keys["a"]}]
        for headers in credentials:
            with self.subTest(header=next(iter(headers)), scheme=headers.get("Authorization", "").split(" ")[0]):
                response = self.client.post("/silences", headers=headers, json=json.loads(self.command().json()))
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.post().status_code, 201)

    def test_unverified_jwt_role_string_does_not_override_verified_group_mapping(self):
        token = actor_token(self, role="viewer", realm_access={"roles": ["admin"]})
        response = self.post(headers={"X-API-KEY": self.service_keys["a"], "X-Keep-Actor-Token": token})
        self.assertEqual(response.status_code, 403)

    def test_debug_headers_never_log_actor_proof_or_service_credentials(self):
        from keep.api.middlewares import LoggingMiddleware
        app = FastAPI()
        app.include_router(self.routes.router, prefix="/integrations/silences")
        app.dependency_overrides = dict(self.client.app.dependency_overrides)
        app.add_middleware(LoggingMiddleware)
        token = actor_token(self)
        with TestClient(app) as client, patch.dict(os.environ, {"LOG_AUTH_PAYLOAD": "true"}), patch("keep.api.middlewares.logger") as logger:
            response = client.post("/integrations/silences", json=json.loads(self.command().json()),
                headers={"X-API-KEY": self.service_keys["a"], "X-Keep-Actor-Token": token})
            self.assertEqual(response.status_code, 201)
        messages = repr(logger.info.call_args_list)
        self.assertTrue(token not in messages and self.service_keys["a"] not in messages)
        self.assertIn("[redacted]", messages)

    def test_configuration_requires_same_active_access_and_strict_references(self):
        cases = []
        changed = copy.deepcopy(self.bundle)
        changed["tenant_id"] = "another"
        cases.append(changed)
        changed = copy.deepcopy(self.bundle)
        changed["service_clients"][0]["scopes"] = ["write:*"]
        cases.append(changed)
        changed = copy.deepcopy(self.bundle)
        changed["proof_profiles"][0]["algorithms"] = ["HS256"]
        cases.append(changed)
        changed = copy.deepcopy(self.bundle)
        changed["subscribers"][0]["destination_refs"] = ["beta-a"]
        cases.append(changed)
        changed = copy.deepcopy(self.bundle)
        changed["dispatch"]["lease_seconds"] = 5
        changed["transports"][0]["delivery"]["timeout_seconds"] = 5
        cases.append(changed)
        import yaml
        for index, document in enumerate(cases):
            with self.subTest(case=index):
                self.config_path.write_text(yaml.safe_dump(document))
                with self.assertRaises(IntegrationConfigurationError):
                    SilenceIntegrations.load(self.config_path)
        self.config_path.write_text(yaml.safe_dump(self.bundle))
        with patch.dict(os.environ, {"KEEP_TEAMS_CONFIG": "version: 1\nteams: []"}):
            get_team_policy.cache_clear()
            with self.assertRaises(IntegrationConfigurationError):
                SilenceIntegrations.load(self.config_path)
        get_team_policy.cache_clear()
