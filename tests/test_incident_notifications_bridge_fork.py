"""Ready bridge protocol over real HTTP, canonical Keep routes and optional lab Mattermost."""

import copy
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlmodel import Session, select

from keep.api.models.db.silence import Silence, SilenceEvent
from tests.test_incident_notifications_api_fork import NotificationApiCase
from tests.test_incident_notifications_postgres_fork import PostgresFixture, DSN


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transport_bridge", ROOT / "transports/mattermost/bridge.py")
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


class BridgeCase(NotificationApiCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"BRIDGE_TRANSPORT_TOKEN": "bridge-local-wire-token",
            "BRIDGE_SERVICE_TOKEN": self.service_keys["a"], "BRIDGE_MM_TOKEN": os.environ.get("MM_BOT_TOKEN", "fixture")}))
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=self.now(1)))
        self.enterContext(patch("keep.api.bl.silences_bl.utc_now", return_value=self.now(1)))
        self.transport_directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.real_mm = os.environ.get("KEEP_BRIDGE_TEST_MM_URL")
        self.channels = ["a" * 26, "b" * 26]
        self.posts, self.mm_writes = {}, []
        self.config = {"schema_version": 1, "tenant_id": "tenant", "transport_ref": "chat-api",
            "keep_api_url": "http://keep.invalid", "keep_ui_url": "https://keep.example.org",
            "mattermost_url": self.real_mm or self.chat.url, "transport_token_ref": "env:BRIDGE_TRANSPORT_TOKEN",
            "service_token_ref": "env:BRIDGE_SERVICE_TOKEN", "mattermost_token_ref": "env:BRIDGE_MM_TOKEN",
            "state_db": str(self.transport_directory / "transport.db"), "timeout_seconds": 3, "recovery_max_pages": 2,
            "destinations": {team + "-chat": {"team_id": team, "channel_id": channel}
                for team, channel in zip(("alpha", "beta"), self.channels)}}
        self.bridge = bridge_module.Bridge(self.config)
        self.configure_http()
        if self.real_mm:
            existing = self.bridge.mm("GET", "/channels/" + os.environ["KEEP_BRIDGE_TEST_CHANNEL"])
            self.channels = [self.bridge.mm("POST", "/channels", {"team_id": existing["team_id"], "type": "P",
                "name": "keep21-" + uuid4().hex, "display_name": "Keep transport 21 test"})["id"] for _ in range(2)]
            for team, channel in zip(("alpha", "beta"), self.channels):
                self.config["destinations"][team + "-chat"]["channel_id"] = channel
        self.server = bridge_module.server(self.bridge, ("127.0.0.1", 0))
        self.start_server()
        self.addCleanup(self.cleanup_bridge)
        self.bundle["service_clients"][0]["team_ids"] = ["alpha", "beta"]
        self.bundle["service_clients"][0].update(scopes=["read:incident", "read:silence", "update:notification"], proof_profile_refs=[])
        self.bundle["transports"][1].update(adapter_ref="mattermost-bridge-v1", endpoint=self.url,
            auth_ref="env:BRIDGE_TRANSPORT_TOKEN", callback_client_ref="client-a")
        for destination in self.bundle["destinations"]:
            if destination["transport_ref"] == "chat-api":
                destination["options"]["channel_id"] = self.config["destinations"][destination["id"]]["channel_id"]
        events = copy.deepcopy(self.bundle["transports"][0])
        events.update(id="silence-events", endpoint=self.url, auth_ref="env:BRIDGE_TRANSPORT_TOKEN")
        self.bundle["transports"].append(events)
        self.bundle["destinations"].extend({"id": team + "-events", "team_id": team,
            "transport_ref": "silence-events", "options": {"path": "/events"}} for team in ("alpha", "beta"))
        self.bundle["subscribers"] = [{"id": "bridge-lifecycle", "team_ids": ["alpha", "beta"],
            "event_types": ["silence." + item for item in ("created", "updated", "activated", "expired", "cancelled")],
            "destination_refs": ["alpha-events", "beta-events"]}]
        self.bundle["presentations"][0]["actions"] = [
            {"command": "ack", "label": "Acknowledge"}, {"command": "silence", "label": "Silence"}]
        self.apply()

    def start_server(self):
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def configure_http(self):
        original = self.bridge.http
        def call(method, url, body=None, headers=None):
            if url.startswith("http://keep.invalid"):
                response = self.client.request(method, url.removeprefix("http://keep.invalid"), json=body, headers=headers)
                if response.is_error:
                    raise urllib.error.HTTPError(url, response.status_code, "Keep rejected request", {}, io.BytesIO(response.content))
                return response.json()
            if not self.real_mm and "/channels/" in url:
                posts = {key: copy.deepcopy(value) for key, value in self.chat.posts.items()
                    if value.get("channel_id") == url.split("/channels/")[1].split("/")[0]}
                return {"order": list(posts), "posts": posts}
            result = original(method, url, body, headers)
            if url.startswith(self.config["mattermost_url"]) and method in {"POST", "PUT"} and "/posts" in url:
                self.posts[result["id"]] = copy.deepcopy(result)
                self.mm_writes.append((method, result["id"]))
            return result
        self.bridge.http = call

    def cleanup_bridge(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.real_mm:
            for channel in self.channels:
                self.bridge.mm("DELETE", "/channels/" + channel)
        self.bridge.db.close()

    def initial(self):
        self.correlate(self.event())
        self.wire_worker().run_once()
        chat = next(row for row in self.deliveries() if row.destination_id == "alpha-chat")
        self.assertEqual(chat.state, "delivered", chat.last_error_code)
        return chat

    def silence(self, *, end=20):
        from keep.api.core.incident_configuration import configuration_scope
        with configuration_scope("tenant"):
            return super().silence(end=end)

    def post(self):
        return self.bridge.mm("GET", "/posts/" + next(iter(self.posts)))

    def latest_event(self):
        with Session(self.engine) as session:
            return session.exec(select(SilenceEvent).order_by(SilenceEvent.revision.desc())).first().payload


class BridgeTransportTest(BridgeCase):
    def test_ready_fields_links_and_no_business_logic(self):
        self.initial()
        post = self.post()
        self.assertEqual(post["props"]["attachments"][0]["title"], "catalog")
        self.assertIn("?command=silence&revision=0", post["props"]["attachments"][0]["footer"])
        self.assertEqual(self.incidents()[0].user_summary, None)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Silence)).all(), [])

    def test_upsert_updates_one_post_after_ack(self):
        self.initial()
        before = self.post()["id"]
        self.change("acknowledged", 2)
        self.wire_worker(3).run_once()
        self.assertEqual(self.post()["id"], before)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.post()["props"]["keep_projection_revision"], 2)

    def test_slow_silence_annotation_does_not_turn_a_confirmed_delivery_unknown(self):
        def slow_annotation(*args):
            threading.Event().wait(2)  # Longer than the sender's one-second response timeout.
            raise TimeoutError("coverage annotation timed out")
        with patch.object(self.bridge, "refresh_silences", side_effect=slow_annotation):
            self.initial()
        self.assertEqual(len(self.posts), 1)

    def test_silence_from_keep_is_visible_and_cancel_does_not_replay(self):
        self.initial()
        self.silence(end=20)
        self.wire_worker(1).run_once()
        post = self.post()
        self.assertEqual(len(post["props"]["keep_silence_ids"]), 1)
        self.assertIn("Automation verification", json.dumps(post["props"]))
        self.change("acknowledged", 2)
        self.wire_worker(3).run_once()
        self.assertEqual(self.post()["props"]["keep_projection_revision"], 1)
        from keep.api.bl.silences_bl import SilencesBL
        from keep.api.models.silence import CancelSilenceCommand
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        actor = AuthenticatedEntity(tenant_id="tenant", email="engineer@example.org", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        from keep.api.core.incident_configuration import configuration_scope
        with configuration_scope("tenant"), Session(self.engine) as session:
            rule = session.exec(select(Silence)).one()
            SilencesBL(session, actor, self.now(4)).cancel(rule.id, CancelSilenceCommand.parse_obj({
                "schema_version": 1, "client_request_id": str(uuid4()), "expected_revision": rule.revision,
                "reason": "Cancel from Keep", "correlation_id": None}))
        self.wire_worker(5).run_once()
        self.assertEqual(self.post()["props"].get("keep_silence_ids") or [], [])
        self.assertNotIn("Automation verification", json.dumps(self.post()["props"]["attachments"]))
        self.assertEqual(self.post()["props"]["keep_projection_revision"], 1)

    def test_old_lifecycle_event_reads_current_state_and_is_idempotent(self):
        self.initial()
        self.silence(end=20)
        self.wire_worker(1).run_once()
        old = self.latest_event()
        self.bridge.event(old)
        count = len(self.mm_writes)
        self.bridge.event(old)
        self.assertEqual(len(self.mm_writes), count)
        with patch("keep.api.bl.silences_evaluator.utc_now", return_value=self.now(21)):
            self.bridge.event(old)
        self.assertEqual(self.post()["props"].get("keep_silence_ids") or [], [])

    def test_lost_post_response_recovers_without_duplicate(self):
        original = self.bridge.mm
        def lost(method, path, body=None):
            result = original(method, path, body)
            if method == "POST" and path == "/posts":
                raise TimeoutError("accepted response lost")
            return result
        self.correlate(self.event())
        with patch.object(self.bridge, "mm", side_effect=lost):
            self.wire_worker().run_once()
        row = next(row for row in self.deliveries() if row.destination_id == "alpha-chat")
        self.assertEqual(row.state, "unknown")
        self.assertEqual(len(self.posts), 1)
        self.bridge.reconcile()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat").state, "delivered")
        self.wire_worker(1).run_once()
        self.assertEqual(len(self.posts), 1)

    def test_restart_restores_unknown_receipt_and_binding(self):
        original = self.bridge.complete
        self.correlate(self.event())
        with patch.object(self.bridge, "complete", side_effect=RuntimeError("crash before receipt commit")):
            self.wire_worker().run_once()
        address = self.server.server_address
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.bridge.db.close()
        self.bridge = bridge_module.Bridge(self.config)
        self.configure_http()
        self.server = bridge_module.server(self.bridge, address)
        self.start_server()
        self.bridge.reconcile()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat").state, "delivered")
        self.assertEqual(len(self.posts), 1)

    def test_transport_auth_legacy_body_and_foreign_channel_are_rejected(self):
        import requests
        response = requests.post(self.url + "/notify", json={"incident_id": str(uuid4())}, timeout=3)
        self.assertEqual(response.status_code, 401)
        response = requests.post(self.url + "/notify", json={"incident_id": str(uuid4())},
            headers={"Authorization": "Bearer bridge-local-wire-token"}, timeout=3)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.mm_writes, [])
        self.initial()
        envelope = json.loads(self.bridge.db.execute("SELECT envelope FROM delivery").fetchone()[0])
        envelope["channel_id"] = self.channels[1]
        with self.assertRaises(bridge_module.BridgeError):
            self.bridge.notify(envelope)
        envelope["channel_id"] = self.channels[0]
        envelope["notification"]["team_id"] = "beta"
        with self.assertRaises(bridge_module.BridgeError) as rejected:
            self.bridge.notify(envelope)
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(len(self.posts), 1)

    def test_duplicate_id_and_changed_body_cannot_create_or_revert_a_post(self):
        self.initial()
        envelope = json.loads(self.bridge.db.execute("SELECT envelope FROM delivery").fetchone()[0])
        count = len(self.mm_writes)
        self.bridge.notify(envelope)
        self.assertEqual(len(self.mm_writes), count)
        envelope["notification"]["title"] = "forged title"
        with self.assertRaises(bridge_module.BridgeError) as rejected:
            self.bridge.notify(envelope)
        self.assertEqual(rejected.exception.code, "idempotency_conflict")
        self.assertEqual(self.post()["props"]["attachments"][0]["title"], "catalog")

    def test_projection_endpoint_requires_the_registered_receiver_and_active_lease(self):
        row = self.initial()
        path = "/integrations/notifications/deliveries/" + str(row.id)
        response = self.client.get(path, headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["allowed"])
        self.assertEqual(response.json()["reason"], "delivery_not_active")
        self.assertIsNone(response.json()["envelope"])
        self.bundle["service_clients"][1]["team_ids"] = ["alpha", "beta"]
        self.apply()
        response = self.client.get(path, headers=self.headers("b", proof=False))
        self.assertEqual(response.status_code, 403)
        self.bundle["transports"][1]["adapter_ref"] = "mattermost-api-v1"
        self.apply()
        response = self.client.get(path, headers=self.headers(proof=False))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.posts), 1)

    def test_missing_silence_event_is_repaired_without_an_ordinary_post(self):
        self.initial()
        self.silence(end=20)
        self.bridge.reconcile()
        self.assertEqual(len(self.post()["props"]["keep_silence_ids"]), 1)
        self.assertEqual(len(self.posts), 1)

    def test_configured_service_card_exists_without_an_incident_post(self):
        self.config["destinations"]["alpha-chat"]["silence_service_posts"] = True
        self.correlate(self.event())
        self.silence(end=20)
        self.wire_worker(1).run_once()
        self.assertEqual(len(self.posts), 1)
        post = self.bridge.mm("GET", "/posts/" + next(iter(self.posts)))
        self.assertNotIn("keep_incident_id", post["props"])
        self.assertEqual(post["props"]["attachments"][0]["title"], "Silence · active")
        self.bridge.event(self.latest_event())
        self.assertEqual(len(self.posts), 1)

    def test_service_card_lost_response_does_not_duplicate_on_recovery(self):
        self.config["destinations"]["alpha-chat"]["silence_service_posts"] = True
        self.correlate(self.event())
        self.silence(end=20)
        original = self.bridge.mm
        def lost(method, path, body=None):
            result = original(method, path, body)
            if method == "POST" and path == "/posts":
                raise TimeoutError("service card response lost")
            return result
        with patch.object(self.bridge, "mm", side_effect=lost):
            self.wire_worker(1).run_once()
        self.assertEqual(len(self.posts), 1)
        self.bridge.reconcile()
        self.wire_worker(2).run_once()
        self.assertEqual(len(self.posts), 1)

    def test_service_expiry_with_same_revision_cannot_confirm_an_old_card(self):
        self.config["destinations"]["alpha-chat"]["silence_service_posts"] = True
        self.correlate(self.event())
        self.silence(end=20)
        self.wire_worker(1).run_once()
        identifier = self.latest_event()["silence_id"]
        original = self.bridge.mm
        self.correlate(self.event("beta-receipt", team="beta"), 2)
        def lost_receipt(method, path, body=None):
            result = original(method, path, body)
            if method == "POST" and path == "/posts" and "keep_notification_id" in body.get("props", {}):
                raise TimeoutError("accepted notification response lost")
            return result
        with patch.object(self.bridge, "mm", side_effect=lost_receipt):
            self.wire_worker(3).run_once()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "beta-chat").state, "unknown")
        def lost_before_update(method, path, body=None):
            if method == "PUT":
                raise TimeoutError("ambiguous update not accepted")
            return original(method, path, body)
        with patch("keep.api.bl.silences_bl.utc_now", return_value=self.now(21)):
            with patch.object(self.bridge, "mm", side_effect=lost_before_update), self.assertRaises(TimeoutError):
                self.bridge.silence_service_post(identifier, "alpha-chat")
            with self.assertRaises(bridge_module.BridgeError) as held:
                self.bridge.silence_service_post(identifier, "alpha-chat")
            self.assertEqual(held.exception.code, "service_delivery_uncertain")
            self.bridge.reconcile()
        self.assertEqual(self.bridge.db.execute("SELECT state FROM silence_post").fetchone()[0], "unknown")
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "beta-chat").state, "delivered")
        self.assertEqual(len(self.posts), 2)

    def test_service_credential_cannot_execute_a_mattermost_silence(self):
        self.initial()
        self.assertIn("?command=silence&revision=0", self.post()["props"]["attachments"][0]["footer"])
        from keep.api.models.silence import utc_string
        response = self.client.post("/integrations/silences", headers=self.headers(proof=False), json={
            "schema_version": 1, "client_request_id": str(uuid4()), "team_id": "alpha",
            "selector": {"kind": "incident", "incident_ids": [str(self.incidents()[0].id)]},
            "starts_at": None, "ends_at": utc_string(self.now(20)), "comment": "MM button", "correlation_id": None})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "forbidden")
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Silence)).all(), [])

    def test_mattermost_silence_link_uses_confirmed_keep_ui_commands(self):
        self.initial()
        from keep.api.routes import silences
        from keep.api.core.incident_configuration import configuration_scope
        from keep.api.models.db.user import User
        from keep.api.models.silence import utc_string
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        app = self.client.app
        app.include_router(silences.router, prefix="/silences")
        actor = AuthenticatedEntity(tenant_id="tenant", email="engineer@example.org", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        for route in app.routes:
            if route.path.startswith("/silences"):
                for dependency in route.dependant.dependencies:
                    if dependency.name == "entity":
                        app.dependency_overrides[dependency.call] = lambda: actor
        with Session(self.engine) as session:
            session.add(User(tenant_id="tenant", username=actor.email, role="responder", password_hash="unused"))
            session.commit()
        # The link selects a canonical incident; only an explicit human command writes.
        with configuration_scope("tenant"), patch("keep.api.bl.silences_bl.utc_now", return_value=self.now(1)):
            response = self.client.post("/silences", json={"schema_version": 1, "client_request_id": str(uuid4()),
                "team_id": "alpha", "selector": {"kind": "incident", "incident_ids": [str(self.incidents()[0].id)]},
                "starts_at": None, "ends_at": utc_string(self.now(20)), "comment": "Confirmed from MM link", "correlation_id": None})
        self.assertEqual(response.status_code, 201, response.text)
        rule = response.json()["result"]
        self.assertEqual(rule["created_by"]["kind"], "user")
        self.wire_worker(1).run_once()
        self.assertIn("Confirmed from MM link", json.dumps(self.post()["props"]["attachments"]))
        with configuration_scope("tenant"), patch("keep.api.bl.silences_bl.utc_now", return_value=self.now(2)):
            response = self.client.post("/silences/" + rule["id"] + "/cancel", json={"schema_version": 1,
                "client_request_id": str(uuid4()), "expected_revision": rule["revision"], "reason": "Confirmed cancel",
                "correlation_id": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.wire_worker(2).run_once()
        self.assertNotIn("Confirmed from MM link", json.dumps(self.post()["props"]["attachments"]))

    def test_refused_mattermost_connection_can_retry_after_recovery(self):
        original = self.bridge.mm
        self.correlate(self.event())
        def refused(method, path, body=None):
            if method == "POST" and path == "/posts":
                raise urllib.error.URLError(ConnectionRefusedError("fixture refused"))
            return original(method, path, body)
        with patch.object(self.bridge, "mm", side_effect=refused):
            self.wire_worker().run_once()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat").state, "pending")
        self.wire_worker(1).run_once()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat").state, "delivered")
        self.assertEqual(len(self.posts), 1)

    def test_silence_annotation_preserves_a_business_field_with_the_same_label(self):
        self.bundle["presentations"][0]["fields"].append({"path": "normalized.workload", "label": "Silences", "order": 2})
        self.apply()
        self.initial()
        self.silence(end=20)
        self.wire_worker(1).run_once()
        card = self.post()["props"]["attachments"][0]
        self.assertEqual(next(field for field in card["fields"] if field["title"] == "Silences")["value"], "catalog")

    def test_lost_update_response_recovers_the_same_post(self):
        self.initial()
        self.change("acknowledged", 2)
        original = self.bridge.mm
        def lost(method, path, body=None):
            result = original(method, path, body)
            if method == "PUT":
                raise TimeoutError("accepted update response lost")
            return result
        with patch.object(self.bridge, "mm", side_effect=lost):
            self.wire_worker(3).run_once()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat" and row.payload["projection_revision"] == 2).state, "unknown")
        self.bridge.reconcile()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-chat" and row.payload["projection_revision"] == 2).state, "delivered")
        self.assertEqual(len(self.posts), 1)

    def test_receiver_checks_silence_after_enqueue_and_fails_closed(self):
        self.correlate(self.event())
        original = self.bridge.keep
        def create_before_gate(method, path, body=None):
            if path.startswith("/integrations/notifications/deliveries/"):
                self.silence(end=20)
            return original(method, path, body)
        with patch.object(self.bridge, "keep", side_effect=create_before_gate):
            self.wire_worker().run_once()
        self.assertEqual(self.posts, {})

    def test_different_teams_with_same_workload_have_different_posts(self):
        self.correlate(self.event())
        self.correlate(self.event("beta-pod", team="beta"))
        self.wire_worker().run_once()
        self.assertEqual(len(self.posts), 2)
        self.assertEqual({p["channel_id"] for p in self.posts.values()}, set(self.channels))
        self.assertEqual(len({p["props"]["keep_incident_id"] for p in self.posts.values()}), 2)

    def test_keep_state_and_other_transport_survive_bridge_outage(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.correlate(self.event())
        self.wire_worker().run_once()
        self.assertEqual(next(row for row in self.deliveries() if row.destination_id == "alpha-http").state, "delivered")
        self.assertEqual(self.incidents()[0].generated_name, "catalog")
        self.correlate(self.event("second", workload="other"))
        self.assertEqual(len(self.incidents()), 2)
        self.server = bridge_module.server(self.bridge, ("127.0.0.1", int(self.url.rsplit(":", 1)[1])))
        self.start_server()


@unittest.skipUnless(DSN, "Requires isolated PostgreSQL in k3d")
class PostgresBridgeTransportTest(PostgresFixture, BridgeTransportTest):
    pass


class BridgeConfigurationTest(unittest.TestCase):
    def test_two_iac_examples_are_valid_and_have_no_service_mutation_scope(self):
        from keep.api.core.incident_contract import read_yaml, validate_bundle
        timings = []
        for variant in ("a", "b"):
            directory = ROOT / "config/incident-bridge.example" / variant
            bundle = read_yaml(directory / "bundle.yaml")
            validate_bundle(bundle, directory, "keep")
            config = json.loads((directory / "bridge.json").read_text())
            with tempfile.TemporaryDirectory() as state:
                config["state_db"] = str(Path(state) / "transport.db")
                bridge = bridge_module.Bridge(config)
                bridge.db.close()
            self.assertEqual(bundle["service_clients"][0]["scopes"], ["read:incident", "read:silence", "update:notification"])
            self.assertEqual(bundle["service_clients"][0]["proof_profile_refs"], [])
            timings.append(config["reconcile_interval_seconds"])
        self.assertNotEqual(*timings)

    def test_adding_mutation_scope_requires_an_operator_proof_profile(self):
        from keep.api.core.incident_contract import ContractError, read_yaml, validate_bundle
        directory = ROOT / "config/incident-bridge.example/a"
        for scope in ("write:silence", "update:silence", "update:incident"):
            bundle = read_yaml(directory / "bundle.yaml")
            bundle["service_clients"][0]["scopes"].append(scope)
            with self.assertRaises(ContractError):
                validate_bundle(bundle, directory, "keep")

    def test_wire_schemas_match_the_canonical_contracts(self):
        wire_spec = importlib.util.spec_from_file_location("wire_builder", ROOT / "transports/mattermost/build-contract.py")
        builder = importlib.util.module_from_spec(wire_spec)
        wire_spec.loader.exec_module(builder)
        shipped = json.loads((ROOT / "transports/mattermost/contract.json").read_text())
        self.assertEqual(shipped["Notification"], builder.wire(ROOT / "keep/api/core/incident_contract_v1.schema.json", "Notification"))
        self.assertEqual(shipped["SilenceEvent"], builder.wire(ROOT / "docs/fork/silences/contract-v1.schema.json", "LifecycleEvent"))
