"""Scoped notification recovery and commands from a registered integration."""

from uuid import UUID

import requests
from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session, select

from keep.api.bl.silences_bl import fail
from keep.api.core.db import get_session
from keep.api.core.incident_automation import snapshot_in, invalid_operation, notification_decision
from keep.api.core.incident_notifications import (
    IncidentNotificationWorker, binding_id, canonical, coverage_reason,
    execute_incident_command, history_safe, notification_history_clause,
    choose_routes, ready,
)
from keep.api.core.incident_lifecycle import lock_incident
from keep.api.core.notification_adapters import verify_receipt
from keep.api.core.silence_integrations import SilenceIntegrations
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_notification import IncidentNotificationBinding
from keep.api.models.db.silence import NotificationDelivery
from keep.api.models.incident_notification import DeliveryReceipt, IncidentCommand
from keep.api.models.silence import utc_now, utc_string
from keep.api.routes.silences import SilenceRoute, reject_impersonation
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.silence_integration_auth import integration_identity
from keep.identitymanager.team_access import require_team_access, visible_team_ids

router = APIRouter(route_class=SilenceRoute, dependencies=[Depends(reject_impersonation)])
read_identity = integration_identity("read:incident")
command_identity = integration_identity("update:incident", delegated=True)
receipt_identity = integration_identity("update:notification")


def current_settings(session, entity):
    snapshot = snapshot_in(session, entity.tenant_id)
    if not snapshot or snapshot["digest"] != entity.integration_configuration_digest:
        fail(409, "configuration_changed", "Integration configuration changed; authenticate again")
    return snapshot, SilenceIntegrations.from_snapshot(snapshot)


def delivery_metadata(row):
    return {"notification_id": str(row.id), "event_id": str(row.event_id), "team_id": row.team_id,
        "incident_id": row.context["incident_id"], "destination_ref": row.destination_id,
        "transport_ref": row.transport_id, "state": row.state, "attempts": row.attempts,
        "last_error_code": row.last_error_code, "projection_revision": row.payload["projection_revision"],
        "available_at": utc_string(row.available_at), "delivered_at": utc_string(row.delivered_at)}


@router.get("")
def snapshot(request: Request, cursor: UUID | None = None, limit: int | None = Query(None, ge=1, le=1000),
    entity: AuthenticatedEntity = Depends(read_identity), session: Session = Depends(get_session)):
    if set(request.query_params) - {"cursor", "limit"}:
        fail(422, "validation_error", "Unknown query parameter")
    _, settings = current_settings(session, entity)
    limit = settings.dispatch["snapshot_page_size"] if limit is None else limit
    if limit > settings.dispatch["snapshot_page_size"]:
        fail(422, "validation_error", "Snapshot limit exceeds configured maximum")
    teams = visible_team_ids(entity)
    query = select(Incident).where(Incident.tenant_id == entity.tenant_id, Incident.is_visible == True,
        Incident.status.in_(("firing", "acknowledged", "resolved")),
        notification_history_clause(entity.tenant_id, Incident.id, Incident.team_id))
    if teams is not None:
        from sqlalchemy import or_
        query = query.where(or_(Incident.team_id.in_([team for team in teams if team is not None]),
            Incident.team_id.is_(None) if None in teams else False))
    if cursor:
        query = query.where(Incident.id > cursor)
    rows = session.exec(query.order_by(Incident.id).limit(limit + 1)).all()
    now = utc_now()
    return {"schema_version": 1, "evaluated_at": utc_string(now),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        "items": [{"incident": canonical(row, now), "projection_revision": (row.notification_context or {}).get("sequence", 0),
                   "notification_gate": coverage_reason(session, row, now) or "allowed"} for row in rows[:limit]]}


@router.get("/deliveries")
def deliveries(request: Request, limit: int = Query(50, ge=1, le=200),
    entity: AuthenticatedEntity = Depends(read_identity), session: Session = Depends(get_session)):
    if set(request.query_params) - {"limit"}:
        fail(422, "validation_error", "Unknown query parameter")
    current_settings(session, entity)
    teams = visible_team_ids(entity)
    # Check canonical ownership, so queue metadata cannot expose a retired owner.
    query = select(NotificationDelivery).where(NotificationDelivery.tenant_id == entity.tenant_id)
    if teams is not None:
        from sqlalchemy import or_
        query = query.where(or_(NotificationDelivery.team_id.in_([team for team in teams if team is not None]),
            NotificationDelivery.team_id.is_(None) if None in teams else False))
    rows = session.exec(query.order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc()).limit(limit)).all()
    items = []
    for row in rows:
        if row.context.get("kind") != "incident":
            continue
        incident = session.get(Incident, UUID(row.context["incident_id"]))
        if incident and incident.tenant_id == entity.tenant_id and incident.team_id == row.team_id and history_safe(session, incident):
            items.append(delivery_metadata(row))
    return {"schema_version": 1, "evaluated_at": utc_string(utc_now()), "items": items}


@router.get("/deliveries/{notification_id}")
def delivery_projection(notification_id: UUID, entity: AuthenticatedEntity = Depends(read_identity),
    session: Session = Depends(get_session)):
    """A receiver checks the current queue/gate before an external effect, including retries."""
    row = session.get(NotificationDelivery, notification_id)
    if not row or row.tenant_id != entity.tenant_id or row.context.get("kind") != "incident":
        fail(404, "not_found", "Not found")
    incident = session.get(Incident, UUID(row.context["incident_id"]))
    if not incident or incident.tenant_id != entity.tenant_id or incident.team_id != row.team_id or not history_safe(session, incident):
        fail(404, "not_found", "Not found")
    require_team_access(entity, incident.team_id)
    snapshot, settings = current_settings(session, entity)
    transport = settings.transports.get(row.transport_id)
    destination = settings.destinations.get(row.destination_id)
    if (not transport or transport.get("adapter_ref") != "mattermost-bridge-v1" or
        transport.get("callback_client_ref") != entity.integration_client_id):
        fail(403, "forbidden", "Receiver is not registered for this transport")
    now = utc_now()
    from keep.api.core.incident_runtime_ownership import gate_reason
    reason = gate_reason(snapshot, incident.team_id, "notifications")
    if not reason and (not incident.is_visible or incident.status in {"merged", "deleted"}):
        reason = "canonical_target_retired"
    if not reason and (not destination or binding_id(incident, destination, transport) != row.context["binding_id"]):
        reason = "destination_changed"
    view = canonical(incident, now)
    routes, route_reason = choose_routes(snapshot, incident, row.payload["event_type"], view)
    if not reason and (route_reason or routes[0]["id"] != row.context["route_id"] or row.destination_id not in routes[0]["destination_refs"]):
        reason = route_reason or "route_changed"
    if not reason and row.context["sequence"] != (incident.notification_context or {}).get("sequence"):
        reason = "superseded_projection"
    if not reason and row.context.get("operation_id"):
        from keep.api.models.db.incident_automation import IncidentAutomationOperation
        operation = session.get(IncidentAutomationOperation, row.context["operation_id"])
        reason = "operation_retired" if not operation or operation.status not in {"awaiting_dispatch", "dispatching"} else (
            invalid_operation(session, incident, operation, snapshot) or notification_decision(incident.automation_context, operation, now))
    if not reason:
        reason = coverage_reason(session, incident, now)
    if not reason and (row.state != "leased" or not row.effect_started or not row.lease_until or row.lease_until <= now):
        reason = "delivery_not_active"
    envelope = None
    if not reason:
        contacts = [address["address"] for contact in snapshot["bundle"].get("contacts", [])
            if contact["id"] in row.payload["contact_refs"] and contact["team_id"] == row.team_id
            for address in contact["addresses"] if address["transport_ref"] == transport["id"]]
        payload = ready(incident, view, snapshot, routes[0], destination, identifier=row.id,
            event_id=row.event_id, event_type=row.payload["event_type"], occurred_at=row.created_at,
            projection_revision=(incident.notification_context or {})["sequence"], contact_refs=row.payload["contact_refs"])
        envelope = {"schema_version": 1, "notification": payload, "channel_id": destination["options"]["channel_id"],
            "contacts": contacts, "delivery_mode": row.context["delivery_mode"], "external_id": row.context.get("external_id")}
    return {"schema_version": 1, "allowed": reason is None, "reason": reason,
        "delivery": delivery_metadata(row), "envelope": envelope}


@router.post("/commands")
def command(body: IncidentCommand, entity: AuthenticatedEntity = Depends(command_identity),
    session: Session = Depends(get_session)):
    return execute_incident_command(session, entity, body)


@router.post("/receipts")
def receipt(body: DeliveryReceipt, entity: AuthenticatedEntity = Depends(receipt_identity),
    session: Session = Depends(get_session)):
    hint = session.get(NotificationDelivery, body.notification_id)
    if not hint or hint.tenant_id != entity.tenant_id or hint.context.get("kind") != "incident":
        fail(404, "not_found", "Not found")
    incident, _ = lock_incident(session, entity.tenant_id, hint.context["incident_id"])
    row = session.exec(select(NotificationDelivery).where(NotificationDelivery.id == body.notification_id).with_for_update()).one()
    require_team_access(entity, incident.team_id)
    if incident.team_id != row.team_id or not history_safe(session, incident):
        fail(404, "not_found", "Not found")
    _, settings = current_settings(session, entity)
    transport, destination = settings.transports.get(row.transport_id), settings.destinations.get(row.destination_id)
    if (not transport or not destination or destination["team_id"] != row.team_id or
        destination["transport_ref"] != row.transport_id or not transport["capabilities"]["receipts"] or
        transport.get("callback_client_ref") != entity.integration_client_id):
        fail(403, "forbidden", "Receipt source is not registered for this transport")
    if body.destination_ref != row.destination_id or body.delivered_revision != row.payload["projection_revision"] and body.status == "delivered":
        fail(409, "receipt_conflict", "Receipt does not match the delivery")
    binding = session.get(IncidentNotificationBinding, row.context["binding_id"])
    if binding_id(incident, destination, transport) != row.context["binding_id"]:
        fail(409, "configuration_changed", "Destination changed; recovery requires the original endpoint")
    if not row.effect_started or row.state not in {"unknown", "delivered"} or not binding:
        fail(409, "receipt_conflict", "Delivery is not awaiting confirmation")
    if body.status != "delivered":
        # An assertion of failure cannot prove that an external create had no effect.
        return {"schema_version": 1, "result": delivery_metadata(row), "replayed": True}
    if row.state == "delivered":
        if row.context.get("confirmed_external_id") != body.external_id:
            fail(409, "receipt_conflict", "Receipt does not match the binding")
        return {"schema_version": 1, "result": delivery_metadata(row), "replayed": True}
    try:
        verified = verify_receipt(row, transport, destination, body.external_id)
    except requests.RequestException:
        fail(503, "receipt_verification_unavailable", "External delivery verification unavailable")
    if not verified:
        fail(409, "unverified_receipt", "External delivery could not be verified")
    IncidentNotificationWorker(session.get_bind(), settings)._complete(session, incident, row,
        {"status": "delivered", "external_id": body.external_id}, utc_now())
    session.commit()
    return {"schema_version": 1, "result": delivery_metadata(row), "replayed": False}
