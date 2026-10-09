"""Protocol adaptation of a ready DTO; no incident grouping, SLA or authorization."""

import json
import re
from urllib.parse import quote

import requests

from keep.api.core.incident_contract import require
from keep.api.core.silence_integrations import secret_value
from keep.providers.http_provider.http_provider import HttpProvider
from keep.providers.mattermost_provider.mattermost_provider import MattermostProvider


CATALOG = {
    "http-json-v1": {"kind": "http_json", "capabilities": {"update": False, "actions": False, "receipts": False}},
    "mattermost-api-v1": {"kind": "mattermost", "capabilities": {"update": True, "actions": False, "receipts": True}},
    "mattermost-api-v2": {"kind": "mattermost", "capabilities": {"update": True, "thread": True, "actions": False, "receipts": True}},
    "mattermost-bridge-v1": {"kind": "mattermost", "capabilities": {"update": True, "actions": False, "receipts": True}},
}


def validate_adapters(bundle):
    transports = {item["id"]: item for item in bundle.get("transports", [])}
    for transport in transports.values():
        profile = CATALOG.get(transport["adapter_ref"])
        require(profile and transport["kind"] == profile["kind"] and transport["capabilities"] == profile["capabilities"],
                "transports.capabilities", "capabilities must match the implemented adapter profile")
        if transport["adapter_ref"] == "mattermost-bridge-v1":
            client = next((item for item in bundle.get("service_clients", []) if item["id"] == transport.get("callback_client_ref")), None)
            require(client and {"read:incident", "read:silence", "update:notification"} <= set(client["scopes"]),
                "transports.callback_client_ref", "bridge requires a scoped projection, silence and receipt client")
            require(all(item["team_id"] in client["team_ids"] for item in bundle.get("destinations", []) if item["transport_ref"] == transport["id"]),
                "transports.callback_client_ref", "bridge client must cover its destination teams")
    for subscriber in bundle.get("subscribers", []):
        destinations = {item["id"]: item for item in bundle.get("destinations", [])}
        require(all(transports[destinations[ref]["transport_ref"]]["adapter_ref"] == "http-json-v1"
                    for ref in subscriber["destination_refs"]), "subscribers", "silence v1 lifecycle delivery requires http-json-v1")


def headers_for(delivery, transport):
    headers = {"Content-Type": "application/json", "X-Keep-Event-ID": str(delivery.event_id),
               "X-Keep-Delivery-ID": str(delivery.id)}
    if transport["auth_ref"]:
        headers["Authorization"] = "Bearer " + secret_value(transport["auth_ref"])
    return headers


def valid_external_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9]{26}", value) is not None


def send_notification(delivery, transport, destination, contacts, binding):
    endpoint = transport["endpoint"].rstrip("/")
    if transport["adapter_ref"] == "http-json-v1":
        return HttpProvider.deliver_notification(endpoint + destination["options"]["path"], delivery.payload,
            headers=headers_for(delivery, transport), timeout=transport["delivery"]["timeout_seconds"])
    addresses = [address["address"] for contact in contacts for address in contact["addresses"]
                 if address["transport_ref"] == transport["id"]]
    if transport["adapter_ref"] == "mattermost-bridge-v1":
        return HttpProvider.deliver_notification(endpoint + "/notify", {
            "schema_version": 1, "notification": delivery.payload,
            "channel_id": destination["options"]["channel_id"], "contacts": addresses,
            "delivery_mode": delivery.context["delivery_mode"], "external_id": binding.external_id,
        }, headers=headers_for(delivery, transport), timeout=transport["delivery"]["timeout_seconds"], receipt=True)
    body = MattermostProvider.notification_payload(delivery.payload, destination["options"]["channel_id"], addresses)
    action = delivery.context.get("effective_action", delivery.context.get("notification_action"))
    external_id = delivery.context.get("target_external_id") if action == "stub" else binding.external_id
    update = bool(external_id and (delivery.context["delivery_mode"] == "upsert" or action == "stub"))
    if action == "thread":
        root = delivery.context.get("root_external_id")
        if not valid_external_id(root):
            return {"status": "failed", "code": "invalid_root_binding"}
        body["root_id"] = root
    if update:
        if not valid_external_id(external_id):
            return {"status": "unknown", "code": "invalid_binding"}
        body["id"] = external_id
    result = HttpProvider.deliver_notification(endpoint + "/api/v4/posts" + ("/" + external_id if update else ""),
        body, headers=headers_for(delivery, transport), timeout=transport["delivery"]["timeout_seconds"],
        method="PUT" if update else "POST", receipt=True)
    if result.get("status") == "delivered" and valid_external_id(result.get("external_id")):
        result["external_url"] = endpoint + "/_redirect/pl/" + result["external_id"]
    return result


def verify_receipt(delivery, transport, destination, external_id):
    if transport["adapter_ref"] not in {"mattermost-api-v1", "mattermost-api-v2", "mattermost-bridge-v1"} or not valid_external_id(external_id):
        return False
    with requests.get(transport["endpoint"].rstrip("/") + "/api/v4/posts/" + quote(external_id, safe=""),
                      headers=headers_for(delivery, transport), allow_redirects=False,
                      timeout=transport["delivery"]["timeout_seconds"], stream=True) as response:
        if response.status_code != 200:
            return False
        raw = response.raw.read(65537, decode_content=True)
        if len(raw) > 65536:
            return False
        try:
            post = json.loads(raw)
        except (ValueError, UnicodeError):
            return False
    if not isinstance(post, dict) or not isinstance(post.get("props"), dict):
        return False
    props = post["props"]
    return (post.get("id") == external_id and post.get("channel_id") == destination["options"]["channel_id"]
            and props.get("keep_notification_id") == str(delivery.id)
            and props.get("keep_incident_id") == delivery.payload["incident_id"]
            and props.get("keep_projection_revision") == delivery.payload["projection_revision"])
