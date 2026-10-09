"""Transactional lifecycle outbox and independent, leased HTTP JSON delivery."""

import json
import logging
import math
from datetime import timedelta
from uuid import uuid4

import requests
from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from keep.api.bl.silences_bl import resource, state_at
from keep.api.core.incident_configuration import configuration_tables_exist
from keep.api.core.silence_integrations import (
    IntegrationConfigurationError, SilenceIntegrations, get_silence_integrations, secret_value,
)
from keep.api.models.db.incident_configuration import IncidentConfiguration
from keep.api.models.db.silence import (
    NotificationDelivery, NotificationTransportBudget, Silence, SilenceEvent,
)
from keep.api.models.silence import SilenceActor, utc_now, utc_string

logger = logging.getLogger(__name__)


def append_silence_event(session, event, settings=None):
    """The caller owns the transaction. Never send or fetch secrets here."""
    settings = settings if settings is not None else get_silence_integrations()
    session.add(event)
    if settings is None or settings.tenant_id != event.tenant_id:
        return
    from keep.api.core.incident_runtime_ownership import gate_reason
    if gate_reason({"bundle": settings.bundle}, event.team_id, "notifications"):
        return
    for subscriber_id, destination in settings.deliveries_for(event):
        session.add(NotificationDelivery(tenant_id=event.tenant_id, team_id=event.team_id,
            event_id=event.event_id, subscriber_id=subscriber_id, destination_id=destination["id"],
            transport_id=destination["transport_ref"], policy_digest=settings.digest,
            payload=event.payload, available_at=event.occurred_at, created_at=event.occurred_at))


def materialize_time_transitions(session, *, now=None, settings=None, limit=100):
    now = now or utc_now()
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    query = select(Silence).where(Silence.cancelled_at.is_(None), Silence.starts_at <= now,
        or_(Silence.last_event_state == "scheduled", and_(Silence.ends_at <= now,
            Silence.last_event_state != "expired")))
    if settings is not None:
        query = query.where(Silence.tenant_id == settings.tenant_id)
    rows = session.exec(query.order_by(Silence.starts_at, Silence.id).limit(limit)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)).all()
    count = 0
    actor = SilenceActor(kind="service", subject="keep-system", issuer=None, display_name="Keep")
    for rule in rows:
        state = state_at(rule, now)
        if state not in {"active", "expired"} or state == rule.last_event_state:
            continue
        revision = rule.revision
        result = session.exec(update(Silence).where(Silence.tenant_id == rule.tenant_id,
            Silence.id == rule.id, Silence.revision == revision, Silence.cancelled_at.is_(None))
            .values(revision=revision + 1, last_event_state=state, updated_at=now,
                    updated_by=actor.dict()).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            continue
        session.refresh(rule)
        event_id = uuid4()
        event_type = "silence.activated" if state == "active" else "silence.expired"
        payload = {"schema_version": 1, "event_id": str(event_id), "event_type": event_type,
            "occurred_at": utc_string(now),
            "effective_at": utc_string(rule.starts_at if state == "active" else rule.ends_at),
            "silence_id": str(rule.id), "revision": rule.revision,
            "tenant_id": rule.tenant_id, "team_id": rule.team_id, "origin": "keep-system",
            "correlation_id": rule.correlation_id, "client_request_id": None,
            "actor": actor.dict(), "reason": "", "resource": json.loads(resource(rule, now).json())}
        append_silence_event(session, SilenceEvent(event_id=event_id, tenant_id=rule.tenant_id,
            team_id=rule.team_id, silence_id=rule.id, revision=rule.revision,
            event_type=event_type, occurred_at=now, payload=payload), settings)
        count += 1
    session.commit()
    return count


class SilenceDeliveryWorker:
    def __init__(self, engine, settings, *, clock=utc_now, sender=None):
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.sender = sender or self._send

    def _refresh_settings(self):
        if configuration_tables_exist(self.engine):
            with Session(self.engine) as session:
                active = session.get(IncidentConfiguration, self.settings.tenant_id)
                if active and active.digest != self.settings.digest:
                    self.settings = SilenceIntegrations.from_snapshot(active.snapshot)

    def _transport(self, delivery):
        settings = self.settings
        subscriber = settings.subscribers.get(delivery.subscriber_id)
        destination = settings.destinations.get(delivery.destination_id)
        if not subscriber or not destination or destination["team_id"] != delivery.team_id:
            return None
        if delivery.team_id not in subscriber["team_ids"] or delivery.payload["event_type"] not in subscriber["event_types"]:
            return None
        if delivery.destination_id not in subscriber["destination_refs"] or destination["transport_ref"] != delivery.transport_id:
            return None
        transport = settings.transports.get(delivery.transport_id)
        return transport if transport and transport["adapter_ref"] == "http-json-v1" else None

    @staticmethod
    def _retry(transport):
        return {"max_attempts": 5, "initial_backoff_seconds": 2,
                "max_backoff_seconds": 60, "multiplier": 2, **transport.get("delivery", {}).get("retry", {})}

    def _budget(self, session, transport, now):
        rate = {"per_second": 5, "burst": 5, **transport.get("delivery", {}).get("rate_limit", {})}
        budget = session.exec(select(NotificationTransportBudget).where(
            NotificationTransportBudget.tenant_id == self.settings.tenant_id,
            NotificationTransportBudget.transport_id == transport["id"])
            .with_for_update().execution_options(populate_existing=True)).first()
        if budget is None:
            budget = NotificationTransportBudget(tenant_id=self.settings.tenant_id,
                transport_id=transport["id"], tokens=float(rate["burst"]), updated_at=now)
            session.add(budget)
            session.flush()
        budget.tokens = min(float(rate["burst"]), budget.tokens +
            max(0, (now - budget.updated_at).total_seconds()) * rate["per_second"])
        budget.updated_at = max(now, budget.updated_at)
        delay = 0 if budget.tokens >= 1 else math.ceil((1 - budget.tokens) / rate["per_second"])
        if not delay:
            budget.tokens -= 1
        session.add(budget)
        return delay

    def claim(self):
        """Claim only the next send; batches never hold leases while waiting behind HTTP."""
        now = self.clock()
        with Session(self.engine, expire_on_commit=False) as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.exec(select(NotificationDelivery).where(
                NotificationDelivery.tenant_id == self.settings.tenant_id,
                NotificationDelivery.state.in_(["pending", "leased"]), NotificationDelivery.available_at <= now,
                or_(NotificationDelivery.lease_until.is_(None), NotificationDelivery.lease_until <= now))
                .order_by(NotificationDelivery.available_at, NotificationDelivery.created_at, NotificationDelivery.id)
                .limit(1).with_for_update(skip_locked=True)).first()
            if row is None:
                return None
            transport = self._transport(row)
            if transport is None:
                row.state, row.last_error_code = "disabled", "subscription_disabled"
                session.add(row)
                session.commit()
                return False
            if row.attempts >= self._retry(transport)["max_attempts"]:
                row.state, row.last_error_code = "failed", "attempts_exhausted_after_crash"
                session.add(row)
                session.commit()
                return False
            try:
                delay = self._budget(session, transport, now)
                if delay:
                    row.available_at = now + timedelta(seconds=delay)
                    session.add(row)
                    session.commit()
                    return False
                row.state, row.lease_token = "leased", uuid4()
                row.lease_until = now + timedelta(seconds=self.settings.dispatch["lease_seconds"])
                row.attempts += 1
                session.add(row)
                session.commit()
                return row
            except IntegrityError:
                # Concurrent first-use budget insertion: retry the next scan, without a send.
                session.rollback()
                return False

    def send_claimed(self, delivery):
        self._refresh_settings()
        transport = self._transport(delivery)
        if transport is None:
            with Session(self.engine) as session:
                session.exec(update(NotificationDelivery).where(
                    NotificationDelivery.id == delivery.id, NotificationDelivery.tenant_id == self.settings.tenant_id,
                    NotificationDelivery.state == "leased", NotificationDelivery.lease_token == delivery.lease_token)
                    .values(state="disabled", lease_token=None, lease_until=None, last_error_code="subscription_disabled"))
                session.commit()
            return False
        # Verify ownership and renew immediately before sending, including slow batch/restart paths.
        now = self.clock()
        with Session(self.engine) as session:
            from keep.api.core.incident_runtime_ownership import gate_reason
            from keep.api.models.db.incident_configuration import IncidentConfiguration
            active = session.get(IncidentConfiguration, delivery.tenant_id) if configuration_tables_exist(self.engine) else None
            reason = gate_reason(active.snapshot if active and active.digest else {"bundle": self.settings.bundle},
                                 delivery.team_id, "notifications")
            if reason:
                session.exec(update(NotificationDelivery).where(
                    NotificationDelivery.id == delivery.id, NotificationDelivery.state == "leased",
                    NotificationDelivery.lease_token == delivery.lease_token).values(
                        state="disabled", lease_token=None, lease_until=None, last_error_code=reason))
                session.commit()
                return False
            result = session.exec(update(NotificationDelivery).where(
                NotificationDelivery.id == delivery.id, NotificationDelivery.tenant_id == self.settings.tenant_id,
                NotificationDelivery.state == "leased", NotificationDelivery.lease_token == delivery.lease_token,
                NotificationDelivery.lease_until > now).values(
                    lease_until=now + timedelta(seconds=self.settings.dispatch["lease_seconds"])))
            session.commit()
            if result.rowcount != 1:
                return False
        try:
            status = self.sender(delivery, transport)
            code = None if 200 <= status < 300 else f"http_{status}"
        except IntegrationConfigurationError:
            code = "credential_unavailable"
        except requests.RequestException:
            code = "transport_unavailable"
        retry = self._retry(transport)
        now = self.clock()
        delay = min(retry["max_backoff_seconds"], retry["initial_backoff_seconds"] *
                    retry["multiplier"] ** max(0, delivery.attempts - 1))
        values = {"state": "delivered" if code is None else
                  ("failed" if delivery.attempts >= retry["max_attempts"] else "pending"),
                  "lease_token": None, "lease_until": None, "last_error_code": code,
                  "delivered_at": now if code is None else None,
                  "available_at": now + timedelta(seconds=delay)}
        with Session(self.engine) as session:
            result = session.exec(update(NotificationDelivery).where(
                NotificationDelivery.id == delivery.id, NotificationDelivery.tenant_id == self.settings.tenant_id,
                NotificationDelivery.state == "leased", NotificationDelivery.lease_token == delivery.lease_token)
                .values(**values))
            session.commit()
        return code is None and result.rowcount == 1

    def _send(self, delivery, transport):
        destination = self.settings.destinations[delivery.destination_id]
        headers = {"Content-Type": "application/json", "X-Keep-Event-ID": str(delivery.event_id),
                   "X-Keep-Delivery-ID": str(delivery.id)}
        if transport["auth_ref"]:
            headers["Authorization"] = "Bearer " + secret_value(transport["auth_ref"])
        endpoint = transport["endpoint"].rstrip("/") + destination["options"]["path"]
        # Redirects never carry credentials or event contents to a different receiver.
        with requests.post(endpoint, json=delivery.payload, headers=headers, allow_redirects=False,
            timeout=transport.get("delivery", {}).get("timeout_seconds", 10), stream=True) as response:
            return response.status_code

    def run_once(self):
        self._refresh_settings()
        with Session(self.engine) as session:
            transitions = materialize_time_transitions(session, settings=self.settings,
                now=self.clock(), limit=self.settings.dispatch["batch_size"])
        sent = 0
        for _ in range(self.settings.dispatch["batch_size"]):
            # Each new delivery observes the published configuration, even if
            # another API worker changed it during this dispatcher cycle.
            self._refresh_settings()
            delivery = self.claim()
            if delivery is None:
                break
            if delivery:
                sent += bool(self.send_claimed(delivery))
        return {"transitions": transitions, "delivered": sent}
