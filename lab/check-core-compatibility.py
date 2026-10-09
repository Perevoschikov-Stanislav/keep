"""Exercise a read-only snapshot of Enterprise's unchanged bridge against fork HTTP routes.

Run in the isolated k3d Job prepared by run-core-compatibility.py. All Keep
data belongs to the test database; the Mattermost boundary is an in-memory fake.
Known legacy limitations are asserted so a green run does not imply safe cutover.
"""

import hashlib
import gc
import importlib.util
import io
import json
import logging
import os
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from keep.api.bl.incident_provisioning import Candidate, IncidentProvisioning
from keep.api.bl.incidents_bl import IncidentBl
from keep.api.core.dependencies import get_pusher_client
from keep.api.core.incident_configuration import reset_configuration_cache
from keep.api.core.incident_runtime_ownership import gate_reason
from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.rule import Rule
from keep.api.models.db.silence import Silence
from keep.api.models.db.tenant import Tenant, TenantApiKey
from keep.api.models.db.user import User
from keep.api.routes import incidents, silences
from keep.identitymanager.authverifierbase import AuthVerifierBase
from keep.identitymanager.identity_managers.oauth2proxy.oauth2proxy_authverifier import Oauth2proxyAuthVerifier
from keep.identitymanager.team_policy import TeamPolicy, get_team_policy
from tests.team_fork_test_case import TeamDatabaseTestCase


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ["KEEP_COMPAT_SOURCE_ROOT"])
EXAMPLE = Path(os.environ.get("KEEP_COMPAT_EXAMPLE_DIR", ROOT / "config/incident-legacy.example"))
TEST_KEY = "isolated-compatibility-key"
USER = "viewer@example.test"


class EnterpriseCompatibilityTest(TeamDatabaseTestCase):
    def setUp(self):
        expected = json.loads((EXAMPLE / "source-manifest.json").read_text())["files"]["keep/bridge/bridge.py"]
        if hashlib.sha256((SOURCE / "bridge/bridge.py").read_bytes()).hexdigest() != expected:
            raise RuntimeError("Enterprise bridge changed; review and update the compatibility snapshot first")
        super().setUp()
        database = os.environ.get("KEEP_COMPAT_TEST_DATABASE")
        if database:
            address = urlsplit(database)
            if address.hostname != "127.0.0.1" or address.path != "/core_compatibility":
                raise RuntimeError("Only the isolated localhost compatibility database is permitted")
            gc.collect()
            schema = "compat_" + uuid4().hex
            control = create_engine(database, poolclass=NullPool)
            with control.begin() as connection:
                connection.exec_driver_sql('CREATE SCHEMA "' + schema + '"')
            control.dispose()
            self.engine = create_engine(database, connect_args={"options": "-csearch_path=" + schema}, poolclass=NullPool)
            self.addCleanup(self.engine.dispose)
            for name in ("db", "alerts", "facets", "incidents"):
                self.enterContext(patch.object(importlib.import_module("keep.api.core." + name), "engine", self.engine))
            # This database is a fresh PostgreSQL sidecar, never the running lab database.
            SQLModel.metadata.create_all(self.engine)
        reset_configuration_cache()
        self.addCleanup(reset_configuration_cache)
        self.enterContext(patch.object(IncidentBl, "send_workflow_event"))
        self.enterContext(patch.object(IncidentBl, "update_client_on_incident_change"))
        with Session(self.engine) as session:
            session.add(Tenant(id="keep", name="Isolated compatibility check"))
            session.add(User(id=1, tenant_id="keep", username=USER, role="viewer", password_hash="unused"))
            session.add(TenantApiKey(tenant_id="keep", reference_id="compatibility-bridge", created_by="bridge@example.test",
                role="admin", key_hash=hashlib.sha256(TEST_KEY.encode()).hexdigest()))
            session.commit()
        app = FastAPI()
        app.include_router(incidents.router, prefix="/incidents")
        app.include_router(silences.router, prefix="/silences")
        app.dependency_overrides[get_pusher_client] = lambda: None
        self.verifiers = {dependency.call for route in app.routes if hasattr(route, "dependant")
                          for dependency in route.dependant.dependencies if isinstance(dependency.call, AuthVerifierBase)}
        self.client = self.enterContext(TestClient(app))
        self.posts, self.calls, self.errors = {}, [], []
        self.enterContext(patch.dict(os.environ, {
            "KEEP_API_URL": "http://keep.invalid", "KEEP_API_KEY": TEST_KEY, "KEEP_UI_URL": "http://ui.invalid",
            "MM_URL": "http://mm.invalid", "MM_BOT_TOKEN": "isolated", "BRIDGE_SECRET": "isolated",
            "STATE_DB": ":memory:", "MM_ROUTES": "OPS/customer-a/*=ops-local;IT/*=it-local;*=fallback-local",
        }))
        spec = importlib.util.spec_from_file_location("core_bridge_" + uuid4().hex, SOURCE / "bridge/bridge.py")
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.addCleanup(self.bridge.DB.close)
        self.bridge.call = self.call
        self.a = self.incident("alpha", "OPS", "customer-a")
        self.b = self.incident("beta", "IT", "infra-cluster")

    def incident(self, team, zone, cluster):
        now = datetime.utcnow()
        fingerprint = "compat-" + uuid4().hex
        with Session(self.engine) as session:
            rule = Rule(tenant_id="keep", name="compat-" + team, definition={"sql": "true", "params": {}},
                definition_cel="true", grouping_criteria=["zone", "cluster"], timeframe=10800,
                created_by="local-test", creation_time=now, updated_by="local-test", update_time=now)
            session.add(rule)
            session.flush()
            incident = Incident(tenant_id="keep", team_id=team, status="firing", is_candidate=False,
                rule_id=rule.id, rule_fingerprint=zone + "," + cluster, alerts_count=1,
                user_generated_name="Compatibility test", user_summary="", generated_summary="")
            alert = Alert(tenant_id="keep", team_id=team, fingerprint=fingerprint, provider_type="prometheus",
                provider_id="compatibility", timestamp=now, event={"name": "KubePodNotReady", "fingerprint": fingerprint,
                    "status": "firing", "lastReceived": now.isoformat(), "startsAt": now.isoformat(),
                    "zone": zone, "cluster": cluster, "namespace": "compat", "family": "workload", "pod": "app-0",
                    "severity": "warning", "level": "non_critical", "description": "Pod compat/app-0 is not ready",
                    "labels": {"alertname": "KubePodNotReady", "cluster": cluster, "namespace": "compat", "pod": "app-0"},
                    "annotations": {"summary": "Pod not ready"}})
            session.add_all([incident, alert])
            session.flush()
            session.add(LastAlert(tenant_id="keep", fingerprint=fingerprint, alert_id=alert.id,
                timestamp=now, first_timestamp=now))
            session.add(LastAlertToIncident(tenant_id="keep", fingerprint=fingerprint, incident_id=incident.id))
            session.commit()
            return str(incident.id)

    def headers(self, role="responder", team="alpha"):
        return {"x-forwarded-email": USER, "x-forwarded-groups": f"/roles/{role},/teams/{team}"}

    def call(self, method, url, body=None, headers=None):
        parsed = urlsplit(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        if parsed.hostname == "keep.invalid":
            response = self.client.request(method, path, json=body, headers=headers)
            self.calls.append((method, path, response.status_code))
            if response.is_error:
                self.errors.append((method, path, response.json()))
                raise urllib.error.HTTPError(path, response.status_code, "Keep rejected request", {}, io.BytesIO(response.content))
            return response.json() if response.content else None
        if parsed.hostname != "mm.invalid":
            raise AssertionError("External transport forbidden in compatibility check")
        path = path.removeprefix("/api/v4")
        if path.startswith("/users/"):
            return {"id": "local-user", "username": "viewer", "email": USER}
        if path.startswith("/channels/"):
            return {"team_id": "local-team"}
        if path.startswith("/teams/"):
            return {"name": "lab"}
        if method == "POST" and path == "/posts":
            post_id = "local-post-" + str(len(self.posts) + 1)
            self.posts[post_id] = {"id": post_id, **body}
            return self.posts[post_id]
        if path.startswith("/posts/"):
            post_id = path.split("/")[2]
            if method == "PUT":
                self.posts[post_id].update(body)
            return self.posts[post_id]
        raise AssertionError("Unexpected Mattermost request")

    def bridge_request(self, path, body, *, secret="isolated"):
        raw = json.dumps(body).encode()
        handler = self.bridge.Handler.__new__(self.bridge.Handler)
        handler.path, handler.rfile = path, io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw)), "X-Bridge-Secret": secret}
        result = []
        handler.answer = lambda code, response: result.append((code, response))
        with patch("sys.stderr", new=io.StringIO()):
            handler.do_POST()
        return result[-1]

    def notify(self, incident_id):
        return self.bridge_request("/notify", {"incident_id": incident_id, "event": "created"})

    def click(self, incident_id, action, **options):
        return self.bridge_request("/action", {"user_id": "local-user", "user_name": "viewer",
            "context": {"secret": "isolated", "incident_id": incident_id, "action": action, **options}})

    def create_silence(self, incident_id):
        response = self.client.post("/silences", headers=self.headers(), json={
            "schema_version": 1, "client_request_id": str(uuid4()), "team_id": "alpha",
            "selector": {"kind": "incident", "incident_ids": [incident_id]}, "starts_at": None,
            "ends_at": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comment": "Isolated compatibility check", "correlation_id": None})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["result"]["id"]

    def test_exact_source_snapshot(self):
        manifest = json.loads((EXAMPLE / "source-manifest.json").read_text())
        for rel, expected in manifest["files"].items():
            path = SOURCE.parent / rel
            if path.exists():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, rel)

    def test_core_artifacts_preserve_native_rules_and_extraction(self):
        spec = importlib.util.spec_from_file_location("core_keepconf", SOURCE / "config/keepconf.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for rule in module.load_rules():
            self.assertEqual(yaml.safe_load((EXAMPLE / "rules" / (rule["name"] + ".yaml")).read_text()), module.rule_body(rule))
        for rule in module.load_extraction():
            self.assertEqual(yaml.safe_load((EXAMPLE / "extraction" / (rule["name"] + ".yaml")).read_text()), module.extraction_body(rule))
        for source in (SOURCE / "config/mapping").glob("*.yaml"):
            self.assertEqual(yaml.safe_load((EXAMPLE / "mappings" / source.name).read_text()), yaml.safe_load(source.read_text()))

    def test_full_group_paths_and_separate_role_membership_are_required(self):
        document = yaml.safe_load((EXAMPLE / "teams.yaml").read_text())
        self.enterContext(patch.dict(os.environ, {"KEEP_TEAMS_CONFIG": yaml.safe_dump(document)}))
        get_team_policy.cache_clear()
        verifier = Oauth2proxyAuthVerifier(["read:incident"])
        def authenticate(groups):
            return verifier.authenticate(Request({"type": "http", "headers": [
                (b"x-forwarded-email", USER.encode()), (b"x-forwarded-groups", groups.encode())]}), "", None, None)
        entity = authenticate("/keep-viewer,/keep-teams/ops")
        self.assertEqual((entity.role, entity.teams, entity.visible_teams), ("viewer", frozenset({"ops"}), frozenset({"ops"})))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as rejected:
            authenticate("keep-viewer,keep-ops")
        self.assertEqual(rejected.exception.status_code, 403)
        role_only = authenticate("/keep-viewer")
        self.assertEqual(role_only.visible_teams, frozenset())

    def test_unassigned_mapping_is_quarantined_without_group_visibility(self):
        policy = TeamPolicy(yaml.safe_load((EXAMPLE / "teams.yaml").read_text()))
        self.assertEqual(policy.team_for_zone("UNASSIGNED"), "quarantine")
        self.assertIsNone(policy.team_for_zone("unrecognized-zone"))
        self.assertNotIn("quarantine", policy.visible_teams({"ops", "it"}))
        self.assertEqual(policy.teams["quarantine"].groups, frozenset())

    def test_native_core_bundle_validates_and_applies_only_to_isolated_database(self):
        bundle = yaml.safe_load((EXAMPLE / "bundle.yaml").read_text())
        candidate = Candidate.load(bundle, EXAMPLE, "keep")
        self.assertEqual({s: len(bundle[s]) for s in ("mappings", "extraction", "rules", "workflows")},
                         {"mappings": 4, "extraction": 2, "rules": 16, "workflows": 2})
        service = IncidentProvisioning("keep")
        preview = service.preview(candidate)
        self.assertIsNone(service.status()["active_digest"])
        service.apply(candidate, expected_active_digest=preview["active_digest"],
            expected_candidate_digest=preview["candidate_digest"], expected_preview_digest=preview["preview_digest"], actor="local-test")
        self.assertEqual(service.status()["generation"], 1)
        for team in ("ops", "it", "quarantine", None):
            self.assertEqual(gate_reason({"bundle": candidate.bundle}, team, "domain"), "runtime_domain_owned_by_legacy")
            self.assertEqual(gate_reason({"bundle": candidate.bundle}, team, "notifications"), "runtime_notifications_owned_by_legacy")

    def test_legacy_notify_reads_incident_alerts_and_writes_thread_link(self):
        code, result = self.notify(self.a)
        self.assertEqual((code, result["post_of"]), (200, self.a))
        self.assertEqual(next(iter(self.posts.values()))["channel_id"], "ops-local")
        self.assertTrue(all(status < 300 for _, _, status in self.calls), self.calls)
        dto = self.bridge.keep("GET", "/incidents/" + self.a)
        self.assertEqual(dto["enrichments"]["incident_provider"], "mattermost")
        self.assertEqual(self.notify(self.a)[0], 200)
        self.assertEqual(len(self.posts), 1)

    def test_workflow_secret_is_required_by_unchanged_bridge(self):
        self.assertEqual(self.bridge_request("/notify", {"incident_id": self.a}, secret="wrong")[0], 403)
        self.assertEqual(self.posts, {})

    def test_keep_viewer_is_denied_but_legacy_admin_callback_bypasses_role_and_team(self):
        denied = self.client.post(f"/incidents/{self.a}/status", headers=self.headers("viewer"), json={"status": "acknowledged"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get("/incidents/" + self.b, headers=self.headers("viewer")).status_code, 404)
        self.assertEqual(self.notify(self.b)[0], 200)
        code, result = self.click(self.b, "ack")
        self.assertEqual((code, result["ephemeral_text"]), (200, "Acknowledged"))
        dto = self.bridge.keep("GET", "/incidents/" + self.b)
        self.assertEqual((dto["status"], dto["assignee"]), ("acknowledged", USER))

    def test_manual_incidents_from_different_teams_collide_in_legacy_bridge(self):
        with Session(self.engine) as session:
            for identifier in (self.a, self.b):
                row = session.get(Incident, UUID(identifier))
                row.rule_id, row.rule_fingerprint = None, None
                session.add(row)
            session.commit()
        self.assertEqual(self.notify(self.a)[1]["post_of"], self.a)
        self.assertEqual(self.notify(self.b)[1]["post_of"], self.a)
        self.assertEqual(len(self.posts), 1)
        self.bridge.refresh(self.a)
        post = next(iter(self.posts.values()))
        self.assertEqual(post["channel_id"], "ops-local")
        cluster = next(field for field in post["props"]["attachments"][0]["fields"] if field["title"] == "Cluster")
        self.assertIn("infra-cluster", cluster["value"])
        self.assertEqual(self.client.get("/incidents/" + self.b, headers=self.headers("viewer")).status_code, 404)

    def test_legacy_unack_payload_remains_accepted(self):
        self.notify(self.a)
        self.assertEqual(self.click(self.a, "ack")[1]["ephemeral_text"], "Acknowledged")
        self.assertEqual(self.click(self.a, "unack")[1]["ephemeral_text"], "Unacknowledged", self.errors)
        dto = self.bridge.keep("GET", "/incidents/" + self.a)
        self.assertEqual(dto["status"], "firing")
        self.assertFalse(dto["assignee"])
        self.assertTrue(all(status < 300 for _, _, status in self.calls), self.calls)

    def test_disabling_impersonation_keeps_callback_as_service_admin(self):
        for verifier in self.verifiers:
            self.enterContext(patch.object(verifier, "impersonation_enabled", False))
        self.notify(self.b)
        self.assertEqual(self.click(self.b, "ack")[1]["ephemeral_text"], "Acknowledged")
        dto = self.bridge.keep("GET", "/incidents/" + self.b)
        self.assertEqual(dto["assignee"], "bridge@example.test")

    def test_legacy_snooze_creates_no_keep_silence(self):
        self.notify(self.a)
        self.assertEqual(self.click(self.a, "snooze", selected_option="4")[1]["ephemeral_text"], "Silenced for 4 hours")
        self.assertTrue(self.bridge.snoozed(self.bridge.load(self.a)))
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Silence)).all(), [])
        self.assertTrue(all(status < 300 for _, _, status in self.calls), self.calls)

    def test_keep_silence_does_not_stop_legacy_notify_or_cancel_from_unsilence(self):
        identifier = self.create_silence(self.a)
        dto = self.client.get("/incidents/" + self.a, headers=self.headers()).json()
        self.assertEqual(dto["silence"]["coverage"], "full")
        self.assertEqual(self.notify(self.a)[0], 200)
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.bridge.snoozed(self.bridge.load(self.a)))
        self.assertEqual(self.click(self.a, "unsilence")[1]["ephemeral_text"], "Unsilenced")
        current = self.client.get("/silences/" + identifier, headers=self.headers()).json()
        self.assertEqual(current["state"], "active")

    def test_silence_api_rejects_legacy_impersonation(self):
        response = self.client.get("/silences", headers={"X-API-KEY": TEST_KEY, "X-KEEP-USER": USER, "X-KEEP-ROLE": "admin"})
        self.assertEqual(response.status_code, 403)

    def test_legacy_notify_accepts_id_but_ignores_prepared_notification_dto(self):
        fixtures = json.loads((ROOT / "docs/fork/incident-core/fixtures-v1.json").read_text())
        message = next(item["body"] for item in fixtures["messages"] if item["schema"] == "Notification")
        message = {**message, "incident_id": self.a, "title": "Prepared canonical title from Keep"}
        code, result = self.bridge_request("/notify", message)
        self.assertEqual((code, result["post_of"]), (200, self.a))
        post = next(iter(self.posts.values()))
        self.assertNotIn(message["title"], post["props"]["attachments"][0]["title"])
        self.assertNotIn("notification_id", result)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
