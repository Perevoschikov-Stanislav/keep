"""Mattermost delivery of Keep projections. Business state and commands belong to Keep."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


class BridgeError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def secret(reference):
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:])
    elif reference.startswith("file:"):
        value = Path(reference[5:]).read_text().rstrip("\r\n")
    else:
        value = None
    if not value or any(char in value for char in "\r\n\x00"):
        raise BridgeError("credential_unavailable", 503)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def post_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9]{26}", value) is not None


class Bridge:
    def __init__(self, config, schema=None):
        self.config = config
        if config.get("schema_version") != 1 or not config.get("destinations"):
            raise BridgeError("invalid_configuration", 422)
        for key in ("keep_api_url", "keep_ui_url", "mattermost_url"):
            url = urllib.parse.urlsplit(config[key])
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise BridgeError("invalid_configuration", 422)
        if "action_url" in config and config["action_url"]:
            url = urllib.parse.urlsplit(config["action_url"])
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
                raise BridgeError("invalid_configuration", 422)
        self.action_url = config.get("action_url")
        for value in config["destinations"].values():
            if not post_id(value["channel_id"]) or not isinstance(value.get("silence_service_posts", False), bool):
                raise BridgeError("invalid_channel", 422)
        self.timeout = config.get("timeout_seconds", 10)
        self.limit = config.get("max_body_bytes", 524288)
        if not 1 <= self.timeout <= 60 or not 1024 <= self.limit <= 1048576:
            raise BridgeError("invalid_configuration", 422)
        if (not 1 <= config.get("reconcile_interval_seconds", 10) <= 3600 or
            not 1 <= config.get("recovery_page_size", 100) <= 200 or not 1 <= config.get("recovery_max_pages", 10) <= 1000):
            raise BridgeError("invalid_configuration", 422)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(config["state_db"], check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS delivery (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, envelope TEXT NOT NULL,
                state TEXT NOT NULL, external_id TEXT, receipt_sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS binding (
                id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, team_id TEXT,
                destination TEXT NOT NULL, external_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS silence_post (
                id TEXT PRIMARY KEY, silence_id TEXT NOT NULL, destination TEXT NOT NULL,
                state TEXT NOT NULL, external_id TEXT, signature TEXT NOT NULL, body TEXT NOT NULL);
        """)
        schema = schema or json.loads(Path(__file__).with_name("contract.json").read_text())
        self.validators = {name: Draft202012Validator(schema[name], format_checker=FormatChecker())
            for name in ("Notification", "SilenceEvent")}

    def http(self, method, url, body=None, headers=None):
        request = urllib.request.Request(url, method=method,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            data = response.read(self.limit + 1)
        if len(data) > self.limit:
            raise BridgeError("response_too_large", 502)
        return json.loads(data) if data else None

    def keep(self, method, path, body=None):
        return self.http(method, self.config["keep_api_url"].rstrip("/") + path, body,
            {"X-API-KEY": secret(self.config["service_token_ref"])})

    def mm(self, method, path, body=None):
        return self.http(method, self.config["mattermost_url"].rstrip("/") + "/api/v4" + path, body,
            {"Authorization": "Bearer " + secret(self.config["mattermost_token_ref"])})

    def destination(self, notification):
        destination = self.config["destinations"].get(notification["destination_ref"])
        if (not destination or notification["tenant_id"] != self.config["tenant_id"] or
            notification["team_id"] != destination["team_id"] or notification["transport_ref"] != self.config["transport_ref"]):
            raise BridgeError("destination_scope_mismatch", 403)
        return destination

    def key(self, notification, channel):
        return digest([notification["tenant_id"], notification["team_id"], notification["incident_id"],
            notification["destination_ref"], notification["transport_ref"], self.config["mattermost_url"], channel])

    def validate(self, name, value):
        if not isinstance(value, dict) or not self.validators[name].is_valid(value):
            raise BridgeError("invalid_" + name.lower(), 422)

    @staticmethod
    def render(envelope, action_url=None, token_ref=None):
        notification = envelope["notification"]
        links = notification["links"] + [{"label": item["label"], "url": item["keep_url"]} for item in notification["actions"]]
        attachment = {"title": notification["title"], "text": notification["description"],
            "fields": [{"title": item["label"], "value": str(item["value"]) if item["value"] is not None else "unknown",
                "short": True} for item in notification["fields"]],
            "footer": " · ".join(f'[{item["label"]}]({item["url"]})' for item in links)}
        if notification["color"]:
            attachment["color"] = notification["color"]
        if action_url and token_ref and notification.get("actions"):
            secret_key = digest([notification["incident_id"], secret(token_ref)])
            actions = []
            for item in notification["actions"]:
                cmd = item["command"]
                actions.append({
                    "id": cmd,
                    "name": item["label"],
                    "type": "button",
                    "style": "primary" if cmd == "ack" else "default",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "incident_id": notification["incident_id"],
                            "command": cmd,
                            "expected_revision": notification["incident_revision"],
                            "secret": secret_key
                        }
                    }
                })
            if actions:
                attachment["actions"] = actions
        return {"channel_id": envelope["channel_id"], "message": " ".join(envelope["contacts"]), "props": {
            "attachments": [attachment], "keep_notification_id": notification["notification_id"],
            "keep_incident_id": notification["incident_id"], "keep_projection_revision": notification["projection_revision"]}}

    def confirmed(self, post, envelope):
        props = post.get("props", {})
        n = envelope["notification"]
        return (post_id(post.get("id")) and post.get("channel_id") == envelope["channel_id"] and
            props.get("keep_notification_id") == n["notification_id"] and props.get("keep_incident_id") == n["incident_id"] and
            props.get("keep_projection_revision") == n["projection_revision"])

    def complete(self, envelope, external_id):
        n = envelope["notification"]
        with self.db:
            self.db.execute("UPDATE delivery SET state='delivered',external_id=? WHERE id=?", (external_id, n["notification_id"]))
            self.db.execute("INSERT OR REPLACE INTO binding VALUES (?,?,?,?,?)", (
                self.key(n, envelope["channel_id"]), n["incident_id"], n["team_id"], n["destination_ref"], external_id))

    def notify(self, envelope):
        with self.lock:
            if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "notification", "channel_id", "contacts", "delivery_mode", "external_id"}:
                raise BridgeError("invalid_envelope", 422)
            self.validate("Notification", envelope["notification"])
            n = envelope["notification"]
            destination = self.destination(n)
            if (envelope["schema_version"] != 1 or envelope["channel_id"] != destination["channel_id"] or
                envelope["delivery_mode"] not in {"upsert", "append"} or not isinstance(envelope["contacts"], list) or
                any(not isinstance(item, str) for item in envelope["contacts"]) or
                envelope["external_id"] is not None and not post_id(envelope["external_id"])):
                raise BridgeError("invalid_envelope", 422)
            identifier, fingerprint = n["notification_id"], digest(envelope)
            previous = self.db.execute("SELECT * FROM delivery WHERE id=?", (identifier,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise BridgeError("idempotency_conflict")
                if previous["state"] == "delivered":
                    return {"id": previous["external_id"]}
                if previous["state"] == "unknown":
                    raise BridgeError("delivery_uncertain", 503)
            gate = self.keep("GET", "/integrations/notifications/deliveries/" + identifier)
            if not gate["allowed"] or gate["envelope"] != envelope:
                raise BridgeError("canonical_projection_changed")
            body = self.render(envelope, self.action_url, self.config.get("transport_token_ref"))
            update = envelope["delivery_mode"] == "upsert" and envelope["external_id"] is not None
            binding = self.db.execute("SELECT * FROM binding WHERE id=?", (self.key(n, envelope["channel_id"]),)).fetchone()
            if binding and n["projection_revision"] < max((json.loads(row[0])["notification"]["projection_revision"]
                for row in self.db.execute("SELECT envelope FROM delivery WHERE external_id=?", (binding["external_id"],))), default=0):
                raise BridgeError("stale_projection")
            if update:
                post = self.mm("GET", "/posts/" + envelope["external_id"])
                if post["channel_id"] != envelope["channel_id"] or post.get("props", {}).get("keep_incident_id") != n["incident_id"]:
                    raise BridgeError("unverified_binding")
                body["id"] = envelope["external_id"]
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO delivery VALUES (?,?,?,'unknown',NULL,0)",
                    (identifier, fingerprint, json.dumps(envelope)))
            try:
                post = self.mm("PUT" if update else "POST", "/posts" + ("/" + body["id"] if update else ""), body)
                if not self.confirmed(post, envelope):
                    raise BridgeError("invalid_receipt", 503)
                self.complete(envelope, post["id"])
            except urllib.error.HTTPError as error:
                if 400 <= error.code < 500:
                    with self.db:
                        self.db.execute("UPDATE delivery SET state='failed' WHERE id=?", (identifier,))
                    raise BridgeError("mattermost_rejected", 424) from None
                raise BridgeError("delivery_uncertain", 503) from None
            except urllib.error.URLError as error:
                if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
                    with self.db:
                        self.db.execute("UPDATE delivery SET state='failed' WHERE id=?", (identifier,))
                    raise BridgeError("mattermost_unavailable", 424) from None
                raise BridgeError("delivery_uncertain", 503) from None
            # Lifecycle/reconciliation updates annotations outside the ordinary receipt path.
            return {"id": post["id"]}

    def receipt_post(self, identifier):
        with self.lock:
            if not post_id(identifier) or not self.db.execute("SELECT 1 FROM delivery WHERE external_id=? AND state='delivered'", (identifier,)).fetchone():
                raise BridgeError("unknown_post", 404)
            return self.mm("GET", "/posts/" + identifier)

    def refresh_silences(self, team=None):
        """Read current Keep coverage, never use event snapshots to make a command."""
        rows = self.db.execute("SELECT * FROM binding" + (" WHERE team_id IS ?" if team is not None else ""),
            (team,) if team is not None else ()).fetchall()
        for row in rows:
            try:
                effective = self.keep("POST", "/integrations/silences/effective", {"schema_version": 1,
                    "targets": [{"kind": "incident", "incident_id": row["incident_id"]}]})["items"][0]
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue  # Retired ownership never grants access via an old binding.
                raise
            reasons = []
            for reason in effective["reasons"]:
                rule = self.keep("GET", "/integrations/silences/" + reason["silence_id"])
                reasons.append({"id": reason["silence_id"], "revision": rule["revision"], "comment": rule["comment"],
                    "actor": rule["updated_by"]["display_name"], "ends_at": rule["ends_at"], "origin": rule["origin"]})
            signature = digest([effective["coverage"], reasons])
            post = self.mm("GET", "/posts/" + row["external_id"])
            props = post.get("props", {})
            if props.get("keep_incident_id") != row["incident_id"] or props.get("keep_silence_projection") == signature:
                continue
            attachments = props.get("attachments", [])
            if not attachments:
                continue
            management_url = self.config["keep_ui_url"].rstrip("/") + "/silences"
            marker = "[Manage silences](" + management_url + ")"
            index = props.get("keep_silence_attachment_index")
            if isinstance(index, int) and 0 < index < len(attachments) and attachments[index].get("footer") == marker:
                attachments.pop(index)
            props["keep_silence_attachment_index"] = None
            if reasons:
                text = "\n".join(f'{item["actor"]}: {item["comment"]} · {item["ends_at"] or "indefinite"} · {item["origin"]} · {item["id"]}' for item in reasons)
                props["keep_silence_attachment_index"] = len(attachments)
                attachments.append({"title": "Silences", "text": effective["coverage"] + "\n" + text, "footer": marker})
            props["keep_silence_projection"] = signature
            props["keep_silence_ids"] = [item["id"] for item in reasons]
            props["keep_silence_management_url"] = management_url
            if marker not in attachments[0].get("footer", ""):
                attachments[0]["footer"] = attachments[0].get("footer", "") + " · " + marker
            self.mm("PUT", "/posts/" + post["id"], {"id": post["id"], "channel_id": post["channel_id"], "message": post["message"], "props": props})

    def event(self, event):
        with self.lock:
            self.validate("SilenceEvent", event)
            if event["tenant_id"] != self.config["tenant_id"] or event["team_id"] not in {v["team_id"] for v in self.config["destinations"].values()}:
                raise BridgeError("destination_scope_mismatch", 403)
            self.refresh_silences(event["team_id"])
            for ref, destination in self.config["destinations"].items():
                if destination["team_id"] == event["team_id"] and destination.get("silence_service_posts", False):
                    self.silence_service_post(event["silence_id"], ref)
            return {"ok": True}

    def matching_posts(self, channel, matches):
        found = []
        for page in range(self.config.get("recovery_max_pages", 10)):
            result = self.mm("GET", "/channels/" + channel + "/posts?" + urllib.parse.urlencode({
                "page": page, "per_page": self.config.get("recovery_page_size", 100)}))
            found.extend(post for post in result["posts"].values() if matches(post))
            if len(result["order"]) < self.config.get("recovery_page_size", 100):
                break
        return found

    def silence_service_post(self, identifier, reference):
        """Optional rule card, including when no alert/incident post exists."""
        destination = self.config["destinations"].get(reference)
        if not destination or not destination.get("silence_service_posts", False):
            return
        rule = self.keep("GET", "/integrations/silences/" + identifier)
        if rule["tenant_id"] != self.config["tenant_id"] or rule["team_id"] != destination["team_id"]:
            raise BridgeError("destination_scope_mismatch", 403)
        key = digest([rule["tenant_id"], rule["team_id"], identifier, reference, self.config["mattermost_url"], destination["channel_id"]])
        body = {"channel_id": destination["channel_id"], "message": "", "props": {
            "keep_silence_id": identifier, "keep_silence_revision": rule["revision"], "keep_destination_ref": reference,
            "attachments": [{"title": "Silence · " + rule["state"], "text": rule["comment"], "fields": [
                {"title": "Operator", "value": rule["updated_by"]["display_name"], "short": True},
                {"title": "Until", "value": rule["ends_at"] or "indefinite", "short": True},
                {"title": "Source", "value": rule["origin"], "short": True}],
                "footer": "[Manage silences](" + self.config["keep_ui_url"].rstrip("/") + "/silences)"}]}}
        signature = digest(body)
        body["props"]["keep_silence_signature"] = signature
        row = self.db.execute("SELECT * FROM silence_post WHERE id=?", (key,)).fetchone()
        external_id = row["external_id"] if row else None
        confirmed = bool(row and row["state"] == "delivered")
        if row and row["state"] == "unknown":
            intent = json.loads(row["body"])
            matches = self.matching_posts(destination["channel_id"], lambda post:
                post.get("channel_id") == intent["channel_id"] and all(post.get("props", {}).get(name) == intent["props"][name]
                    for name in ("keep_silence_id", "keep_silence_revision", "keep_destination_ref", "keep_silence_signature")))
            if len(matches) != 1:
                raise BridgeError("service_delivery_uncertain", 503)
            external_id = matches[0]["id"]
            with self.db:
                self.db.execute("UPDATE silence_post SET state='delivered',external_id=? WHERE id=?", (external_id, key))
            confirmed = True
        if confirmed and row["signature"] == signature:
            return
        if external_id:
            existing = self.mm("GET", "/posts/" + external_id)
            if existing["channel_id"] != destination["channel_id"] or existing.get("props", {}).get("keep_silence_id") != identifier:
                raise BridgeError("unverified_binding")
            body["id"] = external_id
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO silence_post VALUES (?,?,?,'unknown',?,?,?)", (
                key, identifier, reference, external_id, signature, json.dumps(body)))
        try:
            post = self.mm("PUT" if external_id else "POST", "/posts" + ("/" + external_id if external_id else ""), body)
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                with self.db:
                    self.db.execute("UPDATE silence_post SET state='failed' WHERE id=?", (key,))
            raise BridgeError("service_delivery_unavailable", 503) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
                with self.db:
                    self.db.execute("UPDATE silence_post SET state='failed' WHERE id=?", (key,))
            raise BridgeError("service_delivery_unavailable", 503) from None
        if not post_id(post.get("id")) or post.get("channel_id") != destination["channel_id"] or any(
            post.get("props", {}).get(name) != body["props"][name] for name in ("keep_silence_id", "keep_silence_revision", "keep_destination_ref", "keep_silence_signature")):
            raise BridgeError("invalid_receipt", 503)
        with self.db:
            self.db.execute("UPDATE silence_post SET state='delivered',external_id=? WHERE id=?", (post["id"], key))

    def reconcile(self):
        with self.lock:
            for row in self.db.execute("SELECT * FROM delivery WHERE state='unknown'").fetchall():
                envelope = json.loads(row["envelope"])
                matches = self.matching_posts(envelope["channel_id"], lambda post: self.confirmed(post, envelope))
                if len(matches) == 1:
                    self.complete(envelope, matches[0]["id"])
            receipts = self.db.execute("SELECT * FROM delivery WHERE state='delivered' AND receipt_sent=0").fetchall()
            self.refresh_silences()
            for row in self.db.execute("SELECT silence_id,destination FROM silence_post").fetchall():
                try:
                    self.silence_service_post(row["silence_id"], row["destination"])
                except BridgeError as error:
                    if error.code != "service_delivery_uncertain":
                        raise
                    # Hold this intent without delaying other cards or confirmed receipts.
        # Keep verifies the receipt by calling our GET endpoint; do not hold its lock.
        for row in receipts:
            notification = json.loads(row["envelope"])["notification"]
            try:
                self.keep("POST", "/integrations/notifications/receipts", {"schema_version": 1,
                    "notification_id": row["id"], "destination_ref": notification["destination_ref"], "status": "delivered",
                    "external_id": row["external_id"], "delivered_revision": notification["projection_revision"]})
            except urllib.error.HTTPError as error:
                if error.code in {404, 409}:
                    continue
                raise
            with self.lock, self.db:
                self.db.execute("UPDATE delivery SET receipt_sent=1 WHERE id=?", (row["id"],))

    def action(self, body):
        context = body.get("context") or {}
        incident_id = context.get("incident_id")
        command = context.get("command") or context.get("action")
        expected_revision = context.get("expected_revision")
        if not incident_id or not command or expected_revision is None:
            return {"ephemeral_text": "Invalid action context"}
        expected_secret = digest([incident_id, secret(self.config["transport_token_ref"])])
        if not hmac.compare_digest(str(context.get("secret", "")), expected_secret):
            return {"ephemeral_text": "Button rejected: unauthorized action"}
        if command in {"ack", "unack", "resolve", "assign"}:
            payload = {
                "schema_version": 1,
                "client_request_id": str(uuid.uuid4()),
                "incident_id": incident_id,
                "expected_revision": int(expected_revision),
                "command": command,
                "correlation_id": None
            }
            if command == "assign":
                payload["assignee"] = body.get("user_name")
            try:
                self.keep("POST", f"/incidents/{incident_id}/commands", payload)
                labels = {"ack": "Acknowledged", "unack": "Unacknowledged", "resolve": "Resolved", "assign": "Assigned"}
                return {"ephemeral_text": labels.get(command, f"Command {command} executed")}
            except urllib.error.HTTPError as error:
                if error.code == 409:
                    return {"ephemeral_text": "Incident already changed or command in progress; refreshing..."}
                return {"ephemeral_text": f"Command rejected: HTTP {error.code}"}
            except Exception as error:
                return {"ephemeral_text": f"Failed to execute command: {error}"}
        elif command == "silence":
            return {"ephemeral_text": f"Manage silences in Keep: {self.config['keep_ui_url'].rstrip('/')}/silences"}
        elif command == "ticket":
            return {"ephemeral_text": "Ticket action requested"}
        return {"ephemeral_text": f"Unknown action: {command}"}


def server(bridge, address=("0.0.0.0", 8080)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self):
            self.connection.settimeout(bridge.timeout)
            if self.command == "GET" and self.path == "/healthcheck":
                return self.respond(200, {"ok": True})
            try:
                if self.command == "POST" and self.path == "/action":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= bridge.limit or self.headers.get("Transfer-Encoding"):
                        raise BridgeError("invalid_body", 413)
                    body = json.loads(self.rfile.read(length))
                    return self.respond(200, bridge.action(body))

                expected = "Bearer " + secret(bridge.config["transport_token_ref"])
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                    raise BridgeError("unauthorized", 401)
                if self.command == "GET" and self.path.startswith("/api/v4/posts/"):
                    result = bridge.receipt_post(self.path.removeprefix("/api/v4/posts/"))
                elif self.command == "POST" and self.path in {"/notify", "/events"}:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= bridge.limit or self.headers.get("Transfer-Encoding"):
                        raise BridgeError("invalid_body", 413)
                    body = json.loads(self.rfile.read(length))
                    result = bridge.notify(body) if self.path == "/notify" else bridge.event(body)
                else:
                    raise BridgeError("unknown_route", 404)
                self.respond(200, result)
            except BridgeError as error:
                self.respond(error.status, {"error": error.code})
            except (ValueError, TypeError, KeyError):
                self.respond(422, {"error": "invalid_body"})
            except Exception:
                self.respond(503, {"error": "dependency_unavailable"})

        do_GET = dispatch
        do_POST = dispatch
    return ThreadingHTTPServer(address, Handler)


def main():
    Path(os.environ.get("TMPDIR", "/state/tmp")).mkdir(parents=True, exist_ok=True)
    bridge = Bridge(json.loads(Path(os.environ["BRIDGE_CONFIG_FILE"]).read_text()))
    def recover():
        while True:
            try:
                bridge.reconcile()
            except Exception:
                print("bridge recovery: dependency_unavailable", flush=True)
            threading.Event().wait(bridge.config.get("reconcile_interval_seconds", 10))
    threading.Thread(target=recover, daemon=True).start()
    server(bridge).serve_forever()


if __name__ == "__main__":
    main()
