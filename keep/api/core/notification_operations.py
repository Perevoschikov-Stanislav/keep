"""Configured event operations use the existing durable notification queue."""

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlmodel import select

from keep.api.core.notification_policies import (
    resolve_for_incident, event_context, event_rule, event_decision, selected_actions, render_text,
)
from keep.api.models.db.silence import NotificationDelivery as Delivery


def enqueue_operations(session, incident, view, snapshot, event_type, now, sequence, *, operation=None, event=None):
    from keep.api.core.incident_notifications import (
        choose_routes, digest, binding_id, coverage_reason, history_safe, initial_delivery_reason, ready,
    )
    routes, reason = choose_routes(snapshot, incident, event_type, view)
    if reason:
        return reason
    decisions = []
    destinations = {item["id"]: item for item in snapshot["bundle"]["destinations"]}
    transports = {item["id"]: item for item in snapshot["bundle"]["transports"]}
    silence = "foreign_alert_history" if not history_safe(session, incident) else coverage_reason(session, incident, now)
    for route in routes:
        resolved = resolve_for_incident(snapshot["bundle"], route, view)
        settings = resolved["settings"]
        rule = event_rule(settings, route, event_type)
        context = event_context(view, event_type, event, silenced=bool(silence))
        decision = event_decision(rule, context)
        if decision:
            decisions.append(decision)
            continue
        event_id = uuid5(NAMESPACE_URL, "keep-notification:" + digest([
            incident.tenant_id, str(incident.id), incident.team_id, sequence, event_type, operation.id if operation else None]))
        for ref in route["destination_refs"]:
            destination = destinations[ref]
            if destination["team_id"] != incident.team_id or (operation and ref != operation.target_ref):
                continue
            transport = transports[destination["transport_ref"]]
            actions, _ = selected_actions(rule, settings, transport["capabilities"], context)
            if not actions:
                decisions.append("event_none")
                continue
            key = binding_id(incident, destination, transport)
            blocked = silence or initial_delivery_reason(session, incident, route, key)
            decisions.append(blocked)
            refs = rule.get("contact_refs", []) + (operation.context.get("contact_refs", []) if operation else [])
            steps = []
            for action in actions:
                steps += ["post", "stub"] if action == "repost" else [action]
            previous = None
            for index, action in enumerate(steps):
                identifier = uuid5(event_id, route["id"] + ":" + ref + ":" + str(index))
                if session.get(Delivery, identifier):
                    previous = str(identifier)
                    continue
                payload = ready(incident, view, snapshot, route, destination, identifier=identifier,
                    event_id=event_id, event_type=event_type, occurred_at=now, projection_revision=sequence,
                    contact_refs=refs, event=event)
                row = Delivery(id=identifier, tenant_id=incident.tenant_id, team_id=incident.team_id, event_id=event_id,
                    subscriber_id="operation:" + digest([route["id"], index]), destination_id=ref, transport_id=transport["id"],
                    policy_digest=snapshot["digest"], payload=payload, state="skipped" if blocked else "pending",
                    last_error_code=blocked, available_at=now + timedelta(seconds=rule.get("batch_seconds", 0)), created_at=now,
                    context={"kind": "incident", "incident_id": str(incident.id), "sequence": sequence,
                        "route_id": route["id"], "delivery_mode": "upsert" if action == "edit" else "append", "binding_id": key,
                        "notification_action": action, "depends_on": previous, "final_step": index == len(steps) - 1,
                        "repost": "repost" in actions,
                        "event": context["event"], "contact_refs": refs, "settings_digest": digest(settings),
                        "operation_id": operation.id if operation else None})
                session.add(row)
                previous = str(identifier)
                if operation and index == len(steps) - 1:
                    operation.context = {**operation.context, "notification_delivery_id": str(identifier)}
    return decisions[0] if decisions and all(decisions) else None


def prepare_operation(session, incident, row, snapshot, route, view, binding, now):
    """Check dependency and current policy before acquiring a binding/effect lease."""
    from keep.api.core.incident_notifications import digest
    settings = resolve_for_incident(snapshot["bundle"], route, view)["settings"]
    if digest(settings) != row.context["settings_digest"]:
        return "notification_policy_changed", False
    context = event_context(view, row.payload["event_type"], row.context["event"])
    rule = event_rule(settings, route, row.payload["event_type"])
    reason = event_decision(rule, context)
    if reason:
        return reason, False
    previous = session.get(Delivery, UUID(row.context["depends_on"])) if row.context.get("depends_on") else None
    if row.context.get("depends_on") and (previous is None or previous.state in {"failed", "skipped", "disabled"}):
        return "notification_dependency_failed", False
    if previous and previous.state != "delivered":
        return "notification_dependency_pending", True
    action = row.context["notification_action"]
    root = binding.external_id if binding else None
    values = {"root_external_id": root, "line": render_text(settings.get("lines", {}).get(row.payload["event_type"],
              "{{ incident.summary }}"), context)}
    if action == "post" and row.context.get("repost"):
        old = session.exec(select(Delivery).where(Delivery.tenant_id == row.tenant_id,
            Delivery.destination_id == row.destination_id, Delivery.state == "delivered")
            .order_by(Delivery.delivered_at.desc()).limit(100)).all()
        values["previous_external_id"] = root
        values["previous_notification"] = next((item.payload for item in old
            if item.context.get("confirmed_external_id") == root and item.context.get("notification_action") not in {"thread", "stub"}), None)
    if action == "stub":
        values["target_external_id"] = previous.context.get("previous_external_id")
        values["target_notification"] = previous.context.get("previous_notification")
        target = {"title": previous.payload["title"], "posted_at": previous.delivered_at.isoformat() + "Z",
                  "url": previous.context.get("external_url") or previous.payload["keep_url"]}
        values["stub"] = {"keep_card": False, **settings.get("stub", {})}
        values["line"] = render_text(values["stub"].get("text", "{{ target.url }}"), {**context, "target": target})
        values["noop"] = not values["target_external_id"]
    if action == "thread" and not root:
        fallback = settings.get("fallbacks", {}).get("thread")
        if fallback == "post":
            values["effective_action"] = "post"
        elif fallback == "none":
            values["noop"] = True
        else:
            return "notification_root_unavailable", False
    row.context = {**row.context, **values}
    return None, False


def operation_payload(row):
    """The adapter receives fully rendered content and explicit target identities."""
    payload = dict(row.payload)
    action = row.context.get("effective_action", row.context["notification_action"])
    if action == "thread":
        payload.update(description=row.context["line"], fields=[], links=[], actions=[], footer="", tags=[])
    elif action == "stub":
        preserved = row.context.get("target_notification") or {}
        keep_card = row.context["stub"]["keep_card"]
        for key in ("title", "description", "fields", "links", "actions", "color", "footer", "tags"):
            if keep_card and key in preserved:
                payload[key] = preserved[key]
        payload["description"] = (payload["description"] + "\n\n" if keep_card else "") + row.context["line"]
        payload["description"] = payload["description"][:8192]
        if not keep_card:
            payload.update(fields=[], links=[], actions=[], footer="", tags=[])
        if "color" in row.context["stub"]:
            payload["color"] = row.context["stub"]["color"]
    return payload
