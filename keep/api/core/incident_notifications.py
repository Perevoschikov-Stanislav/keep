"""Ready incident projections and routing over the existing notification outbox."""

import copy
import json
import re
from datetime import timedelta
from uuid import UUID, NAMESPACE_URL, uuid5

import requests
from fastapi import HTTPException
from sqlmodel import Session, select

from keep.api.bl.silences_delivery_bl import SilenceDeliveryWorker
from keep.api.bl.silences_evaluator import SilenceEvaluator
from keep.api.core.incident_automation import (
    digest, snapshot_in, sync_incident, invalid_operation, notification_decision, record_result,
)
from keep.api.core.incident_contract import ContractError, validate_shape
from keep.api.core.incident_correlation import matches
from keep.api.core.incident_lifecycle import lock_incident
from keep.api.core.silence_integrations import IntegrationConfigurationError
from keep.api.core.event_normalization import render_presentation, presentation_incident
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_automation import IncidentAutomationOperation as Operation
from keep.api.models.db.incident_notification import (
    IncidentNotificationBinding as Binding, IncidentNotificationCursor as Cursor,
    IncidentIntegrationCommand as CommandReceipt,
)
from keep.api.models.db.silence import NotificationDelivery as Delivery
from keep.api.models.db.tenant import Tenant
from keep.api.models.silence import utc_now, utc_string


def canonical(incident, now):
    from keep.api.core.incident_lifecycle import utc
    context = incident.normalization_context or {}
    if context.get("team_id") != incident.team_id:
        context = {}
    return {**presentation_incident(incident, now=now),
        "created_at": utc_string(utc(incident.creation_time)),
        "normalized": context.get("normalized", {}), "normalization": context.get("normalization", {}),
        "correlation": incident.correlation_context or {}}


def choose_routes(snapshot, incident, event_type, view):
    candidates = []
    data = {**view, "incident": view, "normalized": view["normalized"], "team_id": incident.team_id}
    for route in snapshot["bundle"].get("routes", []):
        if incident.team_id not in route["team_ids"] or event_type not in route["event_types"]:
            continue
        try:
            if matches(route["match"], data):
                candidates.append(route)
        except Exception:
            # An unknown/malformed value never selects a fallback destination.
            continue
    if not candidates:
        return [], "no_route"
    priority = max(item["priority"] for item in candidates)
    selected = [item for item in candidates if item["priority"] == priority]
    return (selected, None) if len(selected) == 1 else ([], "ambiguous_route")


def coverage_reason(session, incident, now):
    coverage = SilenceEvaluator(session, incident.tenant_id, now=now).incidents([incident])[incident.id]
    return "silence_partial_payload_unsafe" if coverage.coverage == "partial" else "silenced" if coverage.silenced else None


def notification_history_clause(tenant_id, incident_id, team_id):
    from sqlalchemy import exists, select as sql_select
    from keep.api.models.db.alert import Alert, LastAlertToIncident
    # IS DISTINCT FROM also handles a NULL canonical owner. NULL never matches
    # a real team; an explicitly configured unassigned destination may read NULL.
    return ~exists(sql_select(Alert.id).join(LastAlertToIncident,
        (Alert.tenant_id == LastAlertToIncident.tenant_id) & (Alert.fingerprint == LastAlertToIncident.fingerprint))
        .where(LastAlertToIncident.tenant_id == tenant_id, LastAlertToIncident.incident_id == incident_id,
               Alert.team_id.is_distinct_from(team_id)))


def history_safe(session, incident):
    return session.exec(select(Incident.id).where(Incident.id == incident.id,
        notification_history_clause(incident.tenant_id, Incident.id, incident.team_id))).first() is not None


def binding_id(incident, destination, transport):
    return digest([incident.tenant_id, incident.team_id, str(incident.id), destination["id"],
                   transport["id"], transport["endpoint"], transport["auth_ref"], destination["options"]])


def initial_delivery_reason(session, incident, route, identifier):
    if route.get("initial_delivery", "all") == "active_only" and incident.status not in {"firing", "acknowledged"}:
        binding = session.get(Binding, identifier)
        if not binding or binding.confirmed_revision == 0:
            return "initial_target_inactive"
    return None


def ready(incident, view, snapshot, route, destination, *, identifier, event_id, event_type, occurred_at,
          projection_revision, contact_refs=None, event=None):
    from keep.api.core.notification_policies import (
        configured, resolve_for_incident, presentation_definition, event_context, condition,
    )
    settings = resolve_for_incident(snapshot["bundle"], route, view)["settings"] if configured(snapshot["bundle"]) else {}
    definition = presentation_definition(snapshot["bundle"], route, settings)
    context = event_context(view, event_type, event)
    metadata = {**view["normalization"], "presentation_ref": definition["id"]}
    rendered = render_presentation(incident.tenant_id, view["normalized"], metadata, view, snapshot=snapshot,
                                   definition=definition, extra=context)
    refs = route["contact_refs"] + (contact_refs or [])
    contacts = [item["id"] for item in snapshot["bundle"].get("contacts", [])
                if item["id"] in refs and item["team_id"] == incident.team_id]
    keep_url = snapshot["bundle"]["keep_url"].rstrip("/") + "/incidents/" + str(incident.id)
    actions = []
    for item in definition["actions"]:
        try:
            if not condition(item.get("when"), context):
                continue
        except Exception:
            continue
        for duration in item.get("options", [None]):
            label = item["label"] + (" (" + str(duration) + "s)" if duration is not None else "")
            link = keep_url + "?command=" + item["command"] + "&revision=" + str(view["revision"])
            if duration is not None:
                link += "&duration_seconds=" + str(duration)
            actions.append({"command": item["command"], "label": label[:128], "keep_url": link,
                **({"mode": "confirm_in_keep", "style": item.get("style", "default")} if settings else {})})
    payload = {"schema_version": 1, "notification_id": str(identifier), "event_id": str(event_id),
        "event_type": event_type, "occurred_at": utc_string(occurred_at), "tenant_id": incident.tenant_id,
        "team_id": incident.team_id, "incident_id": str(incident.id), "incident_revision": view["revision"],
        "projection_revision": projection_revision, "episode": view["episode"], "policy_digest": snapshot["digest"],
        "correlation_id": None, "destination_ref": destination["id"], "transport_ref": destination["transport_ref"],
        "keep_url": keep_url, "title": (incident.user_generated_name or rendered["title"])[:1024],
        "description": (incident.user_summary or rendered["description"])[:8192], "color": rendered["severity_color"],
        "fields": [{"label": item["label"], "value": str(item["value"])[:2048] if item["known"] else None}
                   for item in rendered["fields"]], "links": rendered["links"],
        "contact_refs": contacts, "actions": actions,
        **({"footer": rendered["footer"][:2048]} if "footer" in rendered else {}),
        **({"tags": [tag[:128] for tag in rendered["tags"]]} if "tags" in rendered else {})}
    validate_shape("Notification", payload, "notification")
    return payload


def enqueue(session, incident, view, snapshot, event_type, now, sequence, *, operation=None):
    from keep.api.core.incident_runtime_ownership import gate_reason
    reason = gate_reason(snapshot, incident.team_id, "notifications")
    if reason:
        return reason
    routes, reason = choose_routes(snapshot, incident, event_type, view)
    if reason:
        return reason
    from keep.api.core.notification_policies import configured
    if configured(snapshot["bundle"]):
        from keep.api.core.notification_operations import enqueue_operations
        return enqueue_operations(session, incident, view, snapshot, event_type, now, sequence, operation=operation)
    route = routes[0]
    destinations = {item["id"]: item for item in snapshot["bundle"]["destinations"]}
    transports = {item["id"]: item for item in snapshot["bundle"]["transports"]}
    refs = [ref for ref in route["destination_refs"] if destinations[ref]["team_id"] == incident.team_id
            and (operation is None or ref == operation.target_ref)]
    if not refs:
        return "no_destination"
    silence = "foreign_alert_history" if not history_safe(session, incident) else coverage_reason(session, incident, now)
    event_id = uuid5(NAMESPACE_URL, "keep-notification:" + digest([
        incident.tenant_id, str(incident.id), incident.team_id, sequence, event_type, operation.id if operation else None]))
    decisions = []
    for ref in refs:
        destination, transport = destinations[ref], transports[destinations[ref]["transport_ref"]]
        binding_key = binding_id(incident, destination, transport)
        decision = silence or initial_delivery_reason(session, incident, route, binding_key)
        decisions.append(decision)
        identifier = uuid5(event_id, route["id"] + ":" + ref)
        if session.get(Delivery, identifier):
            continue
        mode = route["delivery_mode"] if transport["capabilities"]["update"] else "append"
        payload = ready(incident, view, snapshot, route, destination, identifier=identifier, event_id=event_id,
                        event_type=event_type, occurred_at=now, projection_revision=sequence,
                        contact_refs=operation.context.get("contact_refs", []) if operation else [])
        row = Delivery(id=identifier, tenant_id=incident.tenant_id, team_id=incident.team_id, event_id=event_id,
            subscriber_id=route["id"], destination_id=ref, transport_id=transport["id"], policy_digest=snapshot["digest"],
            payload=payload, state="skipped" if decision else "pending", last_error_code=decision,
            available_at=now + timedelta(seconds=transport["delivery"]["debounce_seconds"]), created_at=now,
            context={"kind": "incident", "incident_id": str(incident.id), "sequence": sequence,
                "route_id": route["id"], "delivery_mode": mode, "binding_id": binding_key,
                "operation_id": operation.id if operation else None})
        session.add(row)
        if operation:
            operation.context = {**operation.context, "notification_delivery_id": str(row.id)}
    return decisions[0] if decisions and all(decisions) else None


def projection_signature(view, snapshot):
    value = {key: item for key, item in view.items() if key != "automation"}
    if value.get("flapping"):
        value["flapping"] = {"active": value["flapping"]["active"]}
    return digest([value, snapshot["digest"]])


def record_event(session, incident, event_type, now, *, actor="system", source_id=None, **fields):
    """Record notification intent in the transaction that changes the incident."""
    from keep.api.core.notification_policies import configured
    from keep.api.core.incident_runtime_ownership import gate_reason
    from keep.api.core.incident_configuration import configuration_tables_exist
    if not configuration_tables_exist(session.get_bind()):
        return
    snapshot = snapshot_in(session, incident.tenant_id)
    if not snapshot or not configured(snapshot["bundle"]) or not incident.is_visible or gate_reason(snapshot, incident.team_id, "notifications"):
        return
    previous = copy.deepcopy(incident.notification_context or {})
    sources = previous.get("event_sources", {})
    if source_id is not None and sources.get(event_type) == str(source_id):
        return
    view = canonical(incident, now)
    routes, reason = choose_routes(snapshot, incident, event_type, view)
    if reason:
        return
    from keep.api.core.notification_operations import enqueue_operations
    sequence = previous.get("sequence", 0) + 1
    reason = enqueue_operations(session, incident, view, snapshot, event_type, now, sequence,
                                event={"actor": actor, "occurred_at": utc_string(now), **fields})
    if source_id is not None:
        sources[event_type] = str(source_id)
    incident.notification_context = {**previous, "signature": projection_signature(view, snapshot), "sequence": sequence,
        "status": incident.status, "episode": view["episode"], "team_id": incident.team_id, "event_sources": sources,
        "silence_blocked": coverage_reason(session, incident, now) is not None,
        "last_decision": {"reason": reason or "queued", "event_type": event_type, "at": utc_string(now)}}
    session.add(incident)


def materialize_incident(session, incident, snapshot, now):
    """Read committed canonical state; projection state and shared outbox commit together."""
    if not incident.is_visible or incident.status in {"merged", "deleted"}:
        return
    from keep.api.core.incident_runtime_ownership import gate_reason
    if gate_reason(snapshot, incident.team_id, "notifications"):
        return
    if (incident.normalization_context
            and (incident.normalization_context.get("normalization") or {}).get("presentation_digest") != snapshot["digest"]
            and any(item.get("collections") or item.get("source_links") for item in snapshot["bundle"].get("presentations", []))):
        from keep.api.core.event_normalization import refresh_incident_presentation
        refresh_incident_presentation(incident.tenant_id, incident, session, snapshot=snapshot)
    view = canonical(incident, now)
    # Timers have their own explicit destination operations. A sliding flap window
    # updates the card on activation/reset, without replaying each old counter.
    signature = projection_signature(view, snapshot)
    previous = copy.deepcopy(incident.notification_context or {})
    blocked = coverage_reason(session, incident, now) is not None
    was_blocked = previous.get("silence_blocked",
        previous.get("last_decision", {}).get("reason") in {"silenced", "silence_partial_payload_unsafe"})
    resume = False
    if was_blocked and not blocked and incident.status in {"firing", "acknowledged"}:
        routes, reason = choose_routes(snapshot, incident, "incident.updated", view)
        resume = not reason and routes[0].get("after_silence", "none") == "current_active"
    if signature != previous.get("signature") or resume:
        sequence = previous.get("sequence", 0) + 1
        kind = "updated" if resume else "reopened" if previous and previous.get("episode") != view["episode"] else (
            "acknowledged" if incident.status == "acknowledged" and previous.get("status") != incident.status else
            "resolved" if incident.status == "resolved" and previous.get("status") != incident.status else
            "created" if not previous else "updated")
        reason = enqueue(session, incident, view, snapshot, "incident." + kind, now, sequence)
        incident.notification_context = {**previous, "signature": signature, "sequence": sequence,
            "status": incident.status, "episode": view["episode"], "team_id": incident.team_id,
            "silence_blocked": blocked,
            "last_decision": {"reason": reason or "queued", "at": utc_string(now),
                **({"trigger": "silence_ended"} if resume else {})}}
        session.add(incident)
    elif previous.get("silence_blocked") != blocked:
        # Coverage can change while the canonical state is unchanged. Remember
        # it durably, without replaying skipped rows or restarting automation.
        incident.notification_context = {**previous, "silence_blocked": blocked}
        session.add(incident)
    operations = session.exec(select(Operation).where(Operation.tenant_id == incident.tenant_id,
        Operation.incident_id == incident.id, Operation.target_kind == "destination", Operation.status == "awaiting_dispatch")
        .with_for_update()).all()
    for operation in operations:
        if operation.context.get("notification_queued"):
            delivery = session.get(Delivery, UUID(operation.context["notification_delivery_id"])) if operation.context.get("notification_delivery_id") else None
            if delivery and delivery.state in {"disabled", "failed"}:
                operation.status = "failed" if delivery.state == "failed" else "skipped"
                operation.result, operation.completed_at = {"reason": delivery.last_error_code}, now
                record_result(session, incident, operation, now)
            continue
        sync_incident(session, incident, now=now, snapshot=snapshot)
        reason = ("operation_retired" if operation.status != "awaiting_dispatch" else None) or invalid_operation(
            session, incident, operation, snapshot) or notification_decision(incident.automation_context, operation, now)
        if not reason:
            reason = enqueue(session, incident, view, snapshot,
                "incident.reminder" if operation.kind == "reminder" else "incident.escalated",
                now, incident.notification_context["sequence"], operation=operation)
        operation.context = {**operation.context, "notification_queued": True}
        if reason:
            operation.status, operation.result, operation.completed_at = "skipped", {"reason": reason}, now
            record_result(session, incident, operation, now)
        session.add(operation)


class IncidentNotificationWorker(SilenceDeliveryWorker):
    """One queue/worker for silence service events and ordinary incident projections."""

    def _transport(self, delivery):
        if delivery.context.get("kind") != "incident":
            return super()._transport(delivery)
        destination = self.settings.destinations.get(delivery.destination_id)
        transport = self.settings.transports.get(delivery.transport_id)
        if (not destination or not transport or destination["team_id"] != delivery.team_id
                or destination["transport_ref"] != delivery.transport_id):
            return None
        from keep.api.core.notification_adapters import CATALOG
        return transport if transport["adapter_ref"] in CATALOG else None

    def _scan(self):
        if not self.settings.bundle.get("routes"):
            return
        with Session(self.engine) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            session.exec(select(Tenant).where(Tenant.id == self.settings.tenant_id).with_for_update(key_share=True)).one()
            cursor = session.get(Cursor, self.settings.tenant_id) or Cursor(tenant_id=self.settings.tenant_id)
            query = select(Incident.id).where(Incident.tenant_id == self.settings.tenant_id, Incident.is_visible == True,
                Incident.status.in_(("firing", "acknowledged", "resolved")))
            after = query.where(Incident.id > cursor.incident_id) if cursor.incident_id else query
            identifiers = session.exec(after.order_by(Incident.id).limit(self.settings.dispatch["batch_size"])).all()
            if not identifiers and cursor.incident_id:
                identifiers = session.exec(query.order_by(Incident.id).limit(self.settings.dispatch["batch_size"])).all()
            cursor.incident_id = identifiers[-1] if identifiers else None
            session.add(cursor)
            session.commit()
        for identifier in identifiers:
            with Session(self.engine) as session:
                incident, _ = lock_incident(session, self.settings.tenant_id, identifier)
                snapshot = snapshot_in(session, incident.tenant_id)
                if snapshot:
                    materialize_incident(session, incident, snapshot, self.clock())
                    session.commit()

    def claim(self):
        now = self.clock()
        with Session(self.engine) as session:
            hints = session.exec(select(Delivery.id, Delivery.context).where(Delivery.tenant_id == self.settings.tenant_id,
                Delivery.state == "leased", Delivery.effect_started == True, Delivery.lease_until <= now)).all()
        for identifier, context in hints:
            if context.get("kind") == "incident":
                with Session(self.engine) as session:
                    incident, _ = lock_incident(session, self.settings.tenant_id, UUID(context["incident_id"]))
                    row = session.exec(select(Delivery).where(Delivery.id == identifier).with_for_update()).one()
                    if row.state == "leased" and row.effect_started and row.lease_until <= now:
                        self._complete(session, incident, row, {"status": "unknown", "code": "worker_result_unknown"}, now)
                        session.commit()
        return super().claim()

    def _complete(self, session, incident, row, result, now):
        binding = session.get(Binding, row.context["binding_id"])
        status = result["status"]
        if status == "delivered":
            if row.context.get("notification_action") not in {"thread", "stub"}:
                binding.external_id = result.get("external_id") or binding.external_id
                binding.confirmed_revision = max(binding.confirmed_revision, row.payload["projection_revision"])
            binding.uncertain = False
            row.context = {**row.context, "confirmed_external_id": result.get("external_id"),
                           **({"external_url": result["external_url"]} if result.get("external_url") else {})}
            row.state, row.delivered_at, row.last_error_code = "delivered", now, None
        elif status == "unknown":
            row.state, row.last_error_code = "unknown", result.get("code", "transport_result_unknown")
            if binding:
                binding.uncertain = True
        else:
            transport = self._transport(row)
            retry = self._retry(transport) if transport else {"max_attempts": 1}
            row.state = "pending" if row.attempts < retry["max_attempts"] else "failed"
            row.last_error_code = result.get("code", "transport_rejected")
            row.effect_started = False
            row.available_at = now + timedelta(seconds=min(retry.get("max_backoff_seconds", 1),
                retry.get("initial_backoff_seconds", 1) * retry.get("multiplier", 2) ** max(0, row.attempts - 1)))
        row.lease_token, row.lease_until = None, None
        if binding and status != "unknown":
            binding.active_delivery_id, binding.lease_token, binding.lease_until = None, None, None
        if binding:
            session.add(binding)
        session.add(row)
        if row.context.get("operation_id") and row.state in {"delivered", "failed", "unknown", "skipped", "disabled"} and (
                row.state != "delivered" or row.context.get("final_step", True)):
            operation = session.get(Operation, row.context["operation_id"])
            if operation:
                operation.status = {"delivered": "success", "unknown": "uncertain"}.get(row.state, "skipped" if row.state in {"skipped", "disabled"} else "failed")
                operation.result, operation.completed_at = {"reason": row.last_error_code, "notification_id": str(row.id)}, now
                record_result(session, incident, operation, now)

    def _skip(self, session, incident, row, reason, now):
        row.state, row.last_error_code, row.lease_until, row.lease_token = "skipped", reason, None, None
        session.add(row)
        if row.context.get("operation_id"):
            operation = session.get(Operation, row.context["operation_id"])
            if operation and operation.status in {"awaiting_dispatch", "dispatching"}:
                operation.status, operation.result, operation.completed_at = "skipped", {"reason": reason}, now
                record_result(session, incident, operation, now)

    def send_claimed(self, delivery):
        if delivery.context.get("kind") != "incident":
            return super().send_claimed(delivery)
        self._refresh_settings()
        now = self.clock()
        with Session(self.engine, expire_on_commit=False) as session:
            incident, _ = lock_incident(session, delivery.tenant_id, UUID(delivery.context["incident_id"]))
            snapshot = snapshot_in(session, incident.tenant_id)
            row = session.exec(select(Delivery).where(Delivery.id == delivery.id).with_for_update()).one()
            if row.state != "leased" or row.lease_token != delivery.lease_token or row.lease_until <= now:
                return False
            from keep.api.core.incident_runtime_ownership import gate_reason
            reason = gate_reason(snapshot, incident.team_id, "notifications")
            if reason:
                self._skip(session, incident, row, reason, now)
                session.commit()
                return False
            if not snapshot or incident.team_id != row.team_id or not incident.is_visible or incident.status in {"merged", "deleted"}:
                reason = "canonical_target_retired"
            elif not history_safe(session, incident):
                reason = "foreign_alert_history"
            else:
                from keep.api.core.silence_integrations import SilenceIntegrations
                self.settings = SilenceIntegrations.from_snapshot(snapshot)
                materialize_incident(session, incident, snapshot, now)
                view = canonical(incident, now)
                routes, reason = choose_routes(snapshot, incident, row.payload["event_type"], view)
                if not reason and (routes[0]["id"] != row.context["route_id"] or row.destination_id not in routes[0]["destination_refs"]):
                    reason = "route_changed"
                transport, destination = self._transport(row), self.settings.destinations.get(row.destination_id)
                if not reason and (transport is None or binding_id(incident, destination, transport) != row.context["binding_id"]):
                    reason = "destination_changed"
                if not reason:
                    reason = initial_delivery_reason(session, incident, routes[0], row.context["binding_id"])
                operation = session.get(Operation, row.context["operation_id"]) if row.context.get("operation_id") else None
                if not reason and operation:
                    sync_incident(session, incident, now=now, snapshot=snapshot)
                    reason = invalid_operation(session, incident, operation, snapshot) or notification_decision(incident.automation_context, operation, now)
                    if operation.status not in {"awaiting_dispatch", "dispatching"}:
                        reason = reason or "operation_retired"
                if not reason and operation is None and not row.context.get("notification_action") and row.context["sequence"] != incident.notification_context["sequence"]:
                    reason = "superseded_projection"
                if not reason:
                    reason = coverage_reason(session, incident, now)
            if reason:
                self._skip(session, incident, row, reason, now)
                session.commit()
                return False
            binding = session.get(Binding, row.context["binding_id"])
            if row.context.get("notification_action"):
                from keep.api.core.notification_operations import prepare_operation
                reason, wait = prepare_operation(session, incident, row, snapshot, routes[0], view, binding, now)
                if wait:
                    row.state, row.lease_token, row.lease_until = "pending", None, None
                    row.attempts = max(0, row.attempts - 1)
                    row.available_at = now + timedelta(seconds=self.settings.dispatch["scan_interval_seconds"])
                    session.add(row)
                    session.commit()
                    return False
                if reason:
                    self._skip(session, incident, row, reason, now)
                    session.commit()
                    return False
            if binding is None:
                binding = Binding(id=row.context["binding_id"], tenant_id=incident.tenant_id, team_id=incident.team_id,
                    incident_id=incident.id, destination_id=row.destination_id, transport_id=row.transport_id)
            elif (binding.active_delivery_id and binding.active_delivery_id != row.id and
                  binding.lease_until and binding.lease_until <= now and not binding.uncertain):
                previous = session.exec(select(Delivery).where(Delivery.id == binding.active_delivery_id).with_for_update()).first()
                if previous and previous.state == "leased" and previous.effect_started:
                    self._complete(session, incident, previous, {"status": "unknown", "code": "worker_result_unknown"}, now)
                else:
                    binding.uncertain = True
                    session.add(binding)
            if binding.uncertain or (binding.lease_until and binding.lease_until > now and binding.active_delivery_id != row.id):
                row.state, row.lease_token, row.lease_until = "pending", None, None
                row.attempts = max(0, row.attempts - 1)
                row.available_at = now + timedelta(seconds=self.settings.dispatch["scan_interval_seconds"])
                session.add(row)
                session.commit()
                return False
            row.payload = ready(incident, view, snapshot, routes[0], destination, identifier=row.id, event_id=row.event_id,
                event_type=row.payload["event_type"], occurred_at=row.created_at,
                projection_revision=incident.notification_context["sequence"],
                contact_refs=operation.context.get("contact_refs", []) if operation else row.context.get("contact_refs", []),
                event=row.context.get("event"))
            if row.context.get("notification_action"):
                from keep.api.core.notification_operations import operation_payload
                row.payload = operation_payload(row)
                if row.context.get("noop"):
                    session.add(binding)
                    self._complete(session, incident, row, {"status": "delivered"}, now)
                    session.commit()
                    return True
            row.context = {**row.context, "external_id": binding.external_id}
            row.effect_started = True
            row.lease_until = now + timedelta(seconds=self.settings.dispatch["lease_seconds"])
            binding.active_delivery_id, binding.lease_token, binding.lease_until = row.id, row.lease_token, row.lease_until
            session.add(binding)
            session.add(row)
            session.commit()
        try:
            result = self.sender(row, transport)
        except requests.ConnectTimeout:
            result = {"status": "failed", "code": "connect_timeout"}
        except IntegrationConfigurationError:
            result = {"status": "failed", "code": "credential_unavailable"}
        except Exception:
            result = {"status": "unknown", "code": "transport_result_unknown"}
        if not isinstance(result, dict) or result.get("status") not in {"delivered", "failed", "unknown"}:
            result = {"status": "unknown", "code": "invalid_receipt"}
        elif result["status"] == "delivered" and transport["capabilities"]["receipts"]:
            from keep.api.core.notification_adapters import valid_external_id
            if not valid_external_id(result.get("external_id")):
                result = {"status": "unknown", "code": "invalid_receipt"}
        if (result.get("code") not in {"connect_timeout", "connect_unavailable", "credential_unavailable", "transport_result_unknown", "invalid_receipt",
                "invalid_binding", "receipt_too_large", "transport_rejected"} and
                not (isinstance(result.get("code"), str) and re.fullmatch(r"http_[1-5][0-9]{2}", result["code"]))):
            result = {**result, "code": "transport_rejected" if result["status"] == "failed" else "transport_result_unknown"}
        with Session(self.engine) as session:
            incident, _ = lock_incident(session, delivery.tenant_id, UUID(delivery.context["incident_id"]))
            current = session.exec(select(Delivery).where(Delivery.id == row.id).with_for_update()).one()
            if current.state != "leased" or current.lease_token != row.lease_token:
                return False
            self._complete(session, incident, current, result, self.clock())
            session.commit()
        return result["status"] == "delivered"

    def _send(self, delivery, transport):
        if delivery.context.get("kind") != "incident":
            return super()._send(delivery, transport)
        from keep.api.core.notification_adapters import send_notification
        contacts = [item for item in self.settings.bundle.get("contacts", [])
                    if item["id"] in delivery.payload["contact_refs"] and item["team_id"] == delivery.team_id]
        binding = Binding(id=delivery.context["binding_id"], tenant_id=delivery.tenant_id,
                          incident_id=UUID(delivery.context["incident_id"]), destination_id=delivery.destination_id,
                          transport_id=delivery.transport_id, external_id=delivery.context.get("external_id"))
        return send_notification(delivery, transport, self.settings.destinations[delivery.destination_id], contacts, binding)

    def run_once(self):
        self._refresh_settings()
        self._scan()
        return super().run_once()


def execute_incident_command(session, entity, command, *, now=None, pusher_client=None):
    """Canonical CAS, audit and idempotency receipt share one transaction."""
    from keep.api.core.config import config
    from keep.api.core import incident_lifecycle as life
    from keep.api.bl.silences_bl import fail
    from keep.api.models.silence import SilenceActor
    from keep.api.bl.incidents_bl import IncidentBl
    from keep.api.models.db.incident import IncidentStatus
    if config("KEEP_READ_ONLY", default=False, cast=bool):
        fail(403, "forbidden", "Keep is read only")
    actor = getattr(entity, "verified_silence_actor", None)
    if actor is None and (hasattr(entity, "service_scopes") or (entity.api_key_name is not None and entity.role != "admin")):
        fail(403, "actor_proof_required", "Operator proof is required")
    actor = actor or SilenceActor(kind="user", subject=entity.email or "system", issuer=None, display_name=entity.email or "system")
    if actor.kind != "user":
        fail(403, "actor_proof_required", "Operator proof is required")
    now = now or utc_now()
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    try:
        incident, group = lock_incident(session, entity.tenant_id, command.incident_id, entity)
        snapshot = snapshot_in(session, entity.tenant_id)
        authenticated_digest = getattr(entity, "integration_configuration_digest", None)
        if authenticated_digest and (not snapshot or snapshot["digest"] != authenticated_digest):
            fail(409, "configuration_changed", "Integration configuration changed; authenticate again")
        actor_scope = digest([actor.issuer, actor.subject])
        key = entity.tenant_id, actor_scope, command.client_request_id
        command_hash = digest(json.loads(command.json(exclude_unset=True)))
        receipt = session.get(CommandReceipt, key)
        if receipt:
            if receipt.command_hash != command_hash or receipt.incident_id != incident.id:
                fail(409, "idempotency_conflict", "Command ID has already been used")
            response = {**receipt.response, "replayed": True}
            session.rollback()
            return response
        revision = (incident.lifecycle_context or {}).get("revision", 0)
        if revision != command.expected_revision:
            fail(409, "revision_conflict", "Incident changed; refresh before changing it", revision=revision)
        bl = IncidentBl(entity.tenant_id, session, pusher_client, user=entity.email)
        if command.command == "assign":
            changed = life.assign(session, incident, command.assignee, entity, at=now, expected_revision=command.expected_revision)
        elif command.command == "unack":
            if incident.status != IncidentStatus.ACKNOWLEDGED.value:
                fail(409, "invalid_transition", "Only acknowledged incidents can be unacknowledged")
            changed = bl.transition_status(incident, group, IncidentStatus.FIRING, entity, at=now,
                reason="manual", expected_revision=command.expected_revision)
        else:
            changed = bl.transition_status(incident, group,
                IncidentStatus.ACKNOWLEDGED if command.command == "ack" else IncidentStatus.RESOLVED, entity, at=now,
                reason="manual",
                expected_revision=command.expected_revision)
        if snapshot:
            materialize_incident(session, incident, snapshot, now)
        response = {"schema_version": 1, "client_request_id": str(command.client_request_id),
            "incident_id": str(incident.id), "status": incident.status, "assignee": incident.assignee,
            "revision": (incident.lifecycle_context or {}).get("revision", 0), "replayed": False}
        session.add(CommandReceipt(tenant_id=entity.tenant_id, actor_scope=actor_scope,
            client_request_id=command.client_request_id, incident_id=incident.id, command_hash=command_hash,
            actor=actor.dict(), origin=getattr(entity, "integration_origin", "keep"), response=response, created_at=now))
        session.commit()
        if changed:
            bl.postprocess_incident_change(incident)
        return response
    except Exception:
        session.rollback()
        raise
