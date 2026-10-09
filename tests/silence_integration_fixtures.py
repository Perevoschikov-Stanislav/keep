"""Local proof/receiver fixtures. Private keys and generated tokens stay in memory."""

import hashlib
import json
import os
import secrets
import tempfile
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from keep.api.core.silence_integrations import SilenceIntegrations, get_silence_integrations
from keep.identitymanager import silence_integration_auth
from tests.team_fork_test_case import POLICY


@lru_cache(maxsize=1)
def signing_keys():
    private = {"rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
               "ec": ec.generate_private_key(ec.SECP256R1())}
    public = []
    for kid, key in private.items():
        algorithm = "RS256" if kid == "rsa" else "ES256"
        jwk = json.loads(jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(key.public_key()))
        public.append({**jwk, "kid": kid, "alg": algorithm, "use": "sig"})
    return private, public


class FakeReceiver:
    def __init__(self):
        self.events = []
        self.projection = {}
        self.projected_events = set()
        self.status = 200
        self.redirect_url = None
        self.jwks_status = 200
        self.jwks_requests = 0
        self.public_keys = signing_keys()[1]
        receiver = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                receiver.jwks_requests += 1
                data = json.dumps({"keys": receiver.public_keys}).encode()
                self.send_response(receiver.jwks_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                receiver.events.append({"payload": payload, "path": self.path,
                    "event_id": self.headers.get("X-Keep-Event-ID"),
                    "delivery_id": self.headers.get("X-Keep-Delivery-ID"),
                    "authorization": self.headers.get("Authorization")})
                current = receiver.projection.get(payload["silence_id"])
                if payload["event_id"] not in receiver.projected_events and (
                    current is None or payload["revision"] > current["revision"]
                ):
                    receiver.projection[payload["silence_id"]] = payload["resource"]
                receiver.projected_events.add(payload["event_id"])
                self.send_response(receiver.status)
                if receiver.redirect_url:
                    self.send_header("Location", receiver.redirect_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def configure_integrations(case):
    case.receiver_a, case.receiver_b = FakeReceiver(), FakeReceiver()
    case.addCleanup(case.receiver_a.close)
    case.addCleanup(case.receiver_b.close)
    directory = case.enterContext(tempfile.TemporaryDirectory())
    case.config_directory = Path(directory)
    case.service_keys = {"a": secrets.token_urlsafe(32), "b": secrets.token_urlsafe(32)}
    case.outgoing_key = secrets.token_urlsafe(32)
    policy = POLICY.encode()
    (case.config_directory / "teams.yaml").write_bytes(policy)
    profiles = []
    for suffix, receiver, algorithm in (("a", case.receiver_a, "RS256"), ("b", case.receiver_b, "ES256")):
        profiles.append({"id": "proof-" + suffix, "issuer": receiver.url,
            "audience": "keep-actor", "jwks_url": receiver.url + "/jwks", "algorithms": [algorithm],
            "authorized_parties": ["interactive-" + suffix],
            "required_claims": {"typ": "Bearer", "user_kind": "human"}, "clock_skew_seconds": 0})
    transports = [{"id": "receiver-" + suffix, "kind": "http_json", "adapter_ref": "http-json-v1",
        "endpoint": receiver.url, "auth_ref": None if suffix == "a" else "env:KEEP_TEST_OUTGOING_KEY",
        "capabilities": {"update": False, "actions": False, "receipts": False},
        "delivery": {"timeout_seconds": 1, "retry": {"max_attempts": 3,
            "initial_backoff_seconds": 1, "max_backoff_seconds": 3, "multiplier": 2},
            "rate_limit": {"per_second": 100, "burst": 100}, "debounce_seconds": 60}}
        for suffix, receiver in (("a", case.receiver_a), ("b", case.receiver_b))]
    destinations = [{"id": "alpha-" + suffix, "team_id": "alpha", "transport_ref": "receiver-" + suffix,
        "options": {"path": "/events/alpha"}} for suffix in ("a", "b")]
    destinations += [{"id": "beta-a", "team_id": "beta", "transport_ref": "receiver-a",
                      "options": {"path": "/events/beta"}},
                     {"id": "unassigned-a", "team_id": None, "transport_ref": "receiver-a",
                      "options": {"path": "/events/unassigned"}}]
    event_types = ["silence." + name for name in ("created", "updated", "activated", "cancelled", "expired")]
    case.bundle = {"api_version": "keep.incidents/v1", "kind": "IncidentPolicies", "id": "test-integrations",
        "tenant_id": "keep", "revision": "test-1", "keep_url": "http://127.0.0.1:8000",
        "access": {"path": "teams.yaml", "sha256": hashlib.sha256(policy).hexdigest()},
        "proof_profiles": profiles, "transports": transports, "destinations": destinations,
        "service_clients": [{"id": "client-a", "auth_ref": "env:KEEP_TEST_CLIENT_A", "origin": "integration-one",
            "team_ids": ["alpha"], "proof_profile_refs": ["proof-a"],
            "scopes": ["read:silence", "write:silence", "update:silence"]},
            {"id": "client-b", "auth_ref": "env:KEEP_TEST_CLIENT_B", "origin": "integration-two",
            "team_ids": ["beta", None], "proof_profile_refs": ["proof-b"],
            "scopes": ["read:silence", "write:silence", "update:silence"]}],
        "subscribers": [{"id": "watch-alpha-a", "team_ids": ["alpha"], "event_types": event_types,
            "destination_refs": ["alpha-a"]}, {"id": "watch-alpha-b", "team_ids": ["alpha"],
            "event_types": event_types, "destination_refs": ["alpha-b"]},
            {"id": "watch-other-a", "team_ids": ["beta", None], "event_types": event_types,
             "destination_refs": ["beta-a", "unassigned-a"]}],
        "dispatch": {"scan_interval_seconds": 1, "batch_size": 100, "lease_seconds": 5,
                     "snapshot_page_size": 100}}
    case.config_path = case.config_directory / "bundle.yaml"
    case.config_path.write_text(yaml.safe_dump(case.bundle))
    case.enterContext(patch.dict(os.environ, {"KEEP_SILENCES_INTEGRATIONS_CONFIG_FILE": str(case.config_path),
        "KEEP_TEST_CLIENT_A": case.service_keys["a"], "KEEP_TEST_CLIENT_B": case.service_keys["b"],
        "KEEP_TEST_OUTGOING_KEY": case.outgoing_key}))
    get_silence_integrations.cache_clear()
    case.addCleanup(get_silence_integrations.cache_clear)
    case.enterContext(patch.object(silence_integration_auth, "actor_proof_verifier",
                                  silence_integration_auth.ActorProofVerifier()))
    case.settings = get_silence_integrations()


def actor_token(case, *, client="a", role="responder", team=None, **claims):
    import time
    now = int(time.time())
    team = team or ("alpha" if client == "a" else "beta")
    profile = case.settings.profiles["proof-" + client]
    payload = {"iss": profile["issuer"], "aud": profile["audience"], "sub": "operator-" + client,
        "azp": "interactive-" + client, "typ": "Bearer", "user_kind": "human",
        "iat": now, "exp": now + 120, "groups": ["/roles/" + role, "/teams/" + team], **claims}
    kid, algorithm = ("rsa", "RS256") if client == "a" else ("ec", "ES256")
    return jwt.encode(payload, signing_keys()[0][kid], algorithm=algorithm, headers={"kid": kid})
