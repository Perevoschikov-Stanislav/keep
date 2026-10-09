"""Alertmanager active reconciliation and silence synchronization module.

Reconciles active alerts and silences between central Alertmanager and Keep:
- Resolves ghost alerts when they disappear from Alertmanager (under silences, inhibition, or dropped).
- Guards against mass false resolution via 2-consecutive-miss verification, grace period, and circuit breakers.
- Mirrors Alertmanager silences into Keep Silence models with compiled CEL expressions across all relevant teams.
"""

import copy
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import logging
from urllib.parse import quote
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import requests
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.models.alert import AlertDto, AlertSeverity, AlertStatus
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.silence import Silence, SilenceEvent, AlertmanagerReconciliationState
from keep.api.models.silence import SilenceActor, utc_now, utc_string
from keep.api.core.alertmanager_matchers import UnsupportedSelector, filter_matchers, label_matcher, matchers_to_cel
from keep.api.tasks.process_event_task import process_event


logger = logging.getLogger(__name__)


_lease = ContextVar("keep_alertmanager_lease", default=None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def am_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Alertmanager timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def am_wire_time(value):
    # Alertmanager's strfmt.DateTime GET representation truncates to milliseconds.
    parsed = am_time(value)
    return parsed.replace(microsecond=parsed.microsecond // 1000 * 1000)


def payload_digest(payload):
    return digest({"matchers": sorted([{"name": m["name"], "value": m["value"], "isRegex": m.get("isRegex", False),
                    "isEqual": m.get("isEqual", True)} for m in payload["matchers"]],
                    key=lambda m: (m["name"], m["value"], m["isRegex"], m["isEqual"])),
                   "startsAt": utc_string(am_wire_time(payload["startsAt"])), "endsAt": utc_string(am_wire_time(payload["endsAt"])),
                   "createdBy": payload["createdBy"], "comment": payload["comment"]})


def serialized(function):
    @wraps(function)
    def wrapped(self, session, *args, **kwargs):
        key = (id(session), self.source_id, id(self))
        held = _lease.get()
        if held and held[0] == key:
            return function(self, session, *args, **kwargs)
        token = self._claim(session)
        if token is None:
            return {"skipped": "reconciler_busy"}
        context = _lease.set((key, token))
        try:
            return function(self, session, *args, **kwargs)
        except Exception:
            session.rollback()
            raise
        finally:
            _lease.reset(context)
            session.exec(update(AlertmanagerReconciliationState).where(
                AlertmanagerReconciliationState.tenant_id == self.tenant_id,
                AlertmanagerReconciliationState.source_id == self.source_id,
                AlertmanagerReconciliationState.lease_token == token,
            ).values(lease_token=None, lease_until=None))
            session.commit()
    return wrapped


class AlertmanagerReconciler:
    def __init__(
        self,
        alertmanager_url: str,
        tenant_id: str = SINGLE_TENANT_UUID,
        grace_period_seconds: int = 180,
        consecutive_misses_required: int = 2,
        drop_ratio_threshold: float = 0.5,
        provider_types: tuple[str, ...] = ("prometheus", "alertmanager"),
        clock=None,
        http_client=None,
        team_matchers=None,
        circuit_grace_seconds=180,
        interval_seconds=60,
        lease_seconds=120,
    ):
        self.alertmanager_url = alertmanager_url.rstrip("/")
        self.tenant_id = tenant_id
        self.grace_period_seconds = grace_period_seconds
        self.consecutive_misses_required = consecutive_misses_required
        self.drop_ratio_threshold = drop_ratio_threshold
        self.provider_types = provider_types
        self.clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self.http_client = http_client or requests
        self.consecutive_misses: dict[str, int] = {}
        self.last_seen_am_count: int | None = None
        self.breaker_since = None
        self.breaker_fingerprints = None
        self.team_matchers = {} if team_matchers is None else team_matchers
        self.circuit_grace_seconds = circuit_grace_seconds
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self.source_id = digest([self.tenant_id, self.alertmanager_url])
        if (grace_period_seconds < 0 or consecutive_misses_required < 1 or not 0 < drop_ratio_threshold <= 1
                or circuit_grace_seconds < 0 or interval_seconds < 1 or lease_seconds <= 20):
            raise ValueError("Invalid Alertmanager reconciliation limits")
        if not isinstance(self.team_matchers, dict) or any(not isinstance(key, str) or not key for key in self.team_matchers):
            raise ValueError("Team matchers must map team IDs to matcher branches")
        for scopes in self.team_matchers.values():
            if not isinstance(scopes, list) or not scopes:
                raise ValueError("Team matchers must contain nonempty OR branches")
            for branch in scopes:
                if not isinstance(branch, list) or not branch or any(
                    not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"]
                    or not isinstance(m.get("value"), str) or not m["value"]
                    or m.get("isRegex", False) or not m.get("isEqual", True) for m in branch):
                    raise ValueError("Team matchers require positive, nonempty equality matchers")
        branches = [(team, {m["name"]: m["value"] for m in branch})
                    for team, scopes in self.team_matchers.items() for branch in scopes]
        for index, (team, branch) in enumerate(branches):
            for other_team, other in branches[index + 1:]:
                if team != other_team and not any(name in other and other[name] != value for name, value in branch.items()):
                    raise ValueError("Alertmanager team matcher branches must be disjoint across teams")

    def _claim(self, session):
        now, token = utc_now(), uuid4()
        key = (self.tenant_id, self.source_id)
        if session.get(AlertmanagerReconciliationState, key) is None:
            session.add(AlertmanagerReconciliationState(tenant_id=self.tenant_id, source_id=self.source_id))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # Another worker bootstrapped the same source.
        result = session.exec(update(AlertmanagerReconciliationState).where(
            AlertmanagerReconciliationState.tenant_id == self.tenant_id,
            AlertmanagerReconciliationState.source_id == self.source_id,
            or_(AlertmanagerReconciliationState.lease_until.is_(None), AlertmanagerReconciliationState.lease_until <= now),
        ).values(lease_token=token, lease_until=now + timedelta(seconds=self.lease_seconds)))
        session.commit()
        return token if result.rowcount == 1 else None

    def _fence(self, session):
        held, now = _lease.get(), utc_now()
        result = session.exec(update(AlertmanagerReconciliationState).where(
            AlertmanagerReconciliationState.tenant_id == self.tenant_id,
            AlertmanagerReconciliationState.source_id == self.source_id,
            AlertmanagerReconciliationState.lease_token == held[1],
            AlertmanagerReconciliationState.lease_until > now,
        ).values(lease_until=now + timedelta(seconds=self.lease_seconds)))
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeError("Alertmanager reconciliation lease lost")

    def _renew(self, session):
        self._fence(session)
        session.commit()

    def fetch_alerts(self) -> list[dict[str, Any]]:
        """Fetch all alerts (active + silenced + inhibited) from Alertmanager v2 API."""
        url = f"{self.alertmanager_url}/api/v2/alerts"
        resp = self.http_client.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def fetch_silences(self) -> list[dict[str, Any]]:
        """Fetch all silences from Alertmanager v2 API."""
        url = f"{self.alertmanager_url}/api/v2/silences"
        resp = self.http_client.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    @serialized
    def reconcile_alerts(self, session: Session, am_alerts: list[dict[str, Any]]) -> dict[str, Any]:
        """Detect and resolve ghost alerts in Keep that are absent from Alertmanager."""
        if not isinstance(am_alerts, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("fingerprint"), str) or not row["fingerprint"] for row in am_alerts):
            raise ValueError("Invalid Alertmanager alerts snapshot")
        now = self.clock()
        result = {
            "resolved": [],
            "misses_tracked": {},
            "skipped_grace": [],
            "circuit_broken": False,
        }

        # Query all active firing alerts for configured provider types
        stmt = (
            select(LastAlert, Alert)
            .join(Alert, LastAlert.alert_id == Alert.id)
            .where(
                LastAlert.tenant_id == self.tenant_id,
                Alert.provider_type.in_(self.provider_types),
            )
        )
        firing_pairs = []
        for last_alert, alert in session.exec(stmt).all():
            event = alert.event or {}
            if event.get("status") == "firing" or getattr(alert, "status", None) == "firing":
                firing_pairs.append((last_alert, alert))

        if not firing_pairs:
            self.last_seen_am_count = len(am_alerts)
            self.consecutive_misses.clear()
            self.breaker_since, self.breaker_fingerprints = None, None
            return result

        dropped = (not am_alerts or self.last_seen_am_count is not None and self.last_seen_am_count > 4
                   and len(am_alerts) < self.last_seen_am_count * (1.0 - self.drop_ratio_threshold))
        if dropped:
            observed = sorted(item.get("fingerprint", "") for item in am_alerts)
            if observed != self.breaker_fingerprints:
                self.breaker_since, self.breaker_fingerprints = now, observed
                self.consecutive_misses.clear()
            if (now - self.breaker_since).total_seconds() < self.circuit_grace_seconds:
                result["circuit_broken"] = True
                return result
            # A stable successful lower snapshot establishes the new baseline.
        else:
            self.breaker_since, self.breaker_fingerprints = None, None

        self.last_seen_am_count = len(am_alerts)

        # Collect all active fingerprints in Alertmanager
        am_fingerprints = set()
        for item in am_alerts:
            fp = item.get("fingerprint")
            if fp:
                am_fingerprints.add(fp)

        fps_to_resolve = []
        grace_delta = timedelta(seconds=self.grace_period_seconds)

        for last_alert, alert in firing_pairs:
            fp = last_alert.fingerprint
            event = alert.event or {}
            raw_fp = event.get("fingerprint")

            # Still present in Alertmanager -> clear any recorded miss
            if fp in am_fingerprints or (raw_fp and raw_fp in am_fingerprints):
                self.consecutive_misses.pop(fp, None)
                continue

            # Check grace period
            alert_time = last_alert.timestamp or last_alert.first_timestamp
            if alert_time and (now - alert_time) < grace_delta:
                self.consecutive_misses.pop(fp, None)
                result["skipped_grace"].append(fp)
                continue

            # Record miss
            miss_count = self.consecutive_misses.get(fp, 0) + 1
            self.consecutive_misses[fp] = miss_count
            result["misses_tracked"][fp] = miss_count

            if miss_count >= self.consecutive_misses_required:
                fps_to_resolve.append((fp, last_alert, alert))

        # Cleanup misses for alerts no longer in Keep firing list
        active_fps = {la.fingerprint for la, _ in firing_pairs}
        for tracked_fp in list(self.consecutive_misses.keys()):
            if tracked_fp not in active_fps:
                self.consecutive_misses.pop(tracked_fp, None)

        # Resolve confirmed ghosts
        from keep.identitymanager.team_policy import get_team_policy
        policy = get_team_policy(self.tenant_id)

        for fp, last_alert, alert in fps_to_resolve:
            self._renew(session)
            self.consecutive_misses.pop(fp, None)
            event = copy.deepcopy(alert.event or {})
            labels = copy.deepcopy(event.get("labels", {}))
            annotations = copy.deepcopy(event.get("annotations", {}))
            name = event.get("name") or labels.get("alertname") or fp

            zone = event.get("zone")
            if not zone and policy and alert.team_id and alert.team_id in policy.teams:
                team_obj = policy.teams[alert.team_id]
                if team_obj.zones:
                    zone = sorted(team_obj.zones)[0]

            # Mappings/extraction read public root fields as well as labels. Keep
            # the source projection when generating a new resolved occurrence.
            resolved_dto = AlertDto(**{**event, "id": str(uuid4()), "event_id": None,
                "name": name, "status": AlertStatus.RESOLVED, "fingerprint": fp,
                "lastReceived": now.isoformat() + "Z", "severity": event.get("severity", AlertSeverity.INFO),
                "source": ["prometheus"], "labels": labels, "annotations": annotations,
                "resolved_by": "alertmanager_reconciler", "provider_type": "prometheus", "team_id": alert.team_id,
                "isFullDuplicate": False, "isPartialDuplicate": False, "duplicateReason": None,
                "payload": {**event, "status": "resolved", "resolved_by": "alertmanager_reconciler"}})
            if zone:
                setattr(resolved_dto, "zone", zone)

            logger.info(
                "Resolving ghost alert %s (absent from Alertmanager after %d consecutive cycles)",
                fp,
                self.consecutive_misses_required,
            )

            try:
                process_event(
                    {},
                    self.tenant_id,
                    "prometheus",
                    None,
                    fp,
                    None,
                    None,
                    resolved_dto,
                )
                result["resolved"].append(fp)
            except Exception as ex:
                logger.exception("Failed to process resolved ghost alert %s: %s", fp, ex)

        return result

    def _emit(self, session, rule, event_type, now, *, created=False):
        from keep.api.bl.silences_bl import resource, state_at
        from keep.api.bl.silences_delivery_bl import append_silence_event
        from keep.api.core.incident_automation import snapshot_in
        from keep.api.core.silence_integrations import SilenceIntegrations
        # Own the source lease in the SAME transaction as the canonical event.
        self._fence(session)
        actor = SilenceActor(kind="service", subject="alertmanager:" + self.source_id,
                             issuer=None, display_name="Alertmanager")
        if not created:
            rule.revision += 1
        rule.updated_at, rule.updated_by = now, actor.dict()
        rule.last_event_state = state_at(rule, now)
        session.add(rule)
        session.flush()
        identifier = uuid4()
        payload = {"schema_version": 1, "event_id": str(identifier), "event_type": "silence." + event_type,
            "occurred_at": utc_string(now), "effective_at": utc_string(now), "silence_id": str(rule.id),
            "revision": rule.revision, "tenant_id": rule.tenant_id, "team_id": rule.team_id,
            "origin": "alertmanager", "correlation_id": rule.correlation_id, "client_request_id": None,
            "actor": actor.dict(), "reason": "Alertmanager synchronization", "resource": json.loads(resource(rule, now).json())}
        snapshot = snapshot_in(session, self.tenant_id)
        settings = SilenceIntegrations.from_snapshot(snapshot) if snapshot else None
        append_silence_event(session, SilenceEvent(event_id=identifier, tenant_id=rule.tenant_id,
            team_id=rule.team_id, silence_id=rule.id, revision=rule.revision,
            event_type=payload["event_type"], occurred_at=now, payload=payload), settings)

    @serialized
    def reconcile_silences(self, session: Session, am_silences: list[dict[str, Any]]) -> dict[str, Any]:
        """Import externally owned rules, with the same transactional lifecycle feed."""
        from keep.identitymanager.team_policy import get_team_policy
        now = self.clock()
        result = {"created": [], "updated": [], "cancelled": []}
        policy = get_team_policy(self.tenant_id)
        teams = {None, *(policy.teams if policy else [])}
        teams.update(session.exec(select(Alert.team_id).where(Alert.tenant_id == self.tenant_id).distinct()).all())
        active_ids = set()
        actor = SilenceActor(kind="service", subject="alertmanager:" + self.source_id,
                             issuer=None, display_name="Alertmanager").dict()
        parsed = []
        # Validate the entire source snapshot before changing any mirrors.
        if not isinstance(am_silences, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("status"), dict) for row in am_silences):
            raise ValueError("Invalid Alertmanager silence snapshot")
        for remote in am_silences:
            if remote.get("status", {}).get("state") not in {"active", "pending"} or remote.get("createdBy") == "keep":
                continue
            try:
                identifier = remote["id"]
                starts, ends = am_time(remote["startsAt"]), am_time(remote["endsAt"])
                selector = {"kind": "filter", "cel": matchers_to_cel(remote["matchers"])}
                if ends <= starts or not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                    raise ValueError()
                from keep.api.bl.silences_bl import compile_filter
                if (len(selector["cel"]) > 8192 or not isinstance(remote.get("comment", ""), str)
                        or len(remote.get("comment", "")) > 4000):
                    raise ValueError()
                compile_filter(selector["cel"], alertmanager=True)
            except (KeyError, TypeError, ValueError, AttributeError):
                # Do not cancel a valid existing mirror on malformed input.
                raise ValueError("Invalid Alertmanager silence snapshot") from None
            parsed.append((remote, identifier, starts, ends, selector))
        for remote, identifier, starts, ends, selector in parsed:
            active_ids.add(identifier)
            for team in sorted(teams, key=lambda value: value or ""):
                self._renew(session)
                correlation = identifier + (":" + team if team else "")
                mirror_id = uuid5(NAMESPACE_URL, digest(["keep-alertmanager-mirror", self.source_id, identifier, team]))
                candidates = session.exec(select(Silence).where(Silence.tenant_id == self.tenant_id,
                    Silence.origin == "alertmanager", Silence.correlation_id == correlation)
                    .with_for_update().execution_options(populate_existing=True)).all()
                rule = next((row for row in candidates if row.id == mirror_id or not row.external_context
                             or self.source_id in row.external_context), None)
                comment = remote.get("comment", "Mirrored from Alertmanager")
                if rule is None:
                    rule = Silence(id=mirror_id, tenant_id=self.tenant_id, team_id=team, revision=1, selector=selector,
                        starts_at=starts, ends_at=ends, comment=comment, created_by=actor, updated_by=actor,
                        created_at=now, updated_at=now, origin="alertmanager", correlation_id=correlation,
                        last_event_state="scheduled" if starts > now else "active",
                        external_context={self.source_id: {"state": "imported", "team_id": team, "external_id": identifier}})
                    self._emit(session, rule, "created", now, created=True)
                    result["created"].append(str(rule.id))
                elif rule.cancelled_at is None:
                    changes = {"selector": selector, "starts_at": starts, "ends_at": ends, "comment": comment,
                               "created_by": actor, "updated_by": actor}
                    if any(getattr(rule, key) != value for key, value in changes.items()):
                        for key, value in changes.items():
                            setattr(rule, key, value)
                        rule.external_context = {**(rule.external_context or {}), self.source_id:
                            {"state": "imported", "team_id": team, "external_id": identifier}}
                        self._emit(session, rule, "updated", now)
                        result["updated"].append(str(rule.id))
                session.commit()
        rules = session.exec(select(Silence).where(Silence.tenant_id == self.tenant_id,
            Silence.origin == "alertmanager", Silence.cancelled_at.is_(None))).all()
        for hint in rules:
            context = (hint.external_context or {}).get(self.source_id)
            # Old mirrors have a protected origin but no receipt. Never use a native command correlation as a receipt.
            identifier = context.get("external_id") if context else hint.correlation_id.split(":", 1)[0] if hint.correlation_id else None
            if context is None and hint.external_context:
                continue  # Receipt belongs to a different AM endpoint.
            if identifier not in active_ids:
                self._renew(session)
                rule = session.exec(select(Silence).where(Silence.id == hint.id).with_for_update()
                    .execution_options(populate_existing=True)).one()
                if rule.cancelled_at is None:
                    rule.cancelled_at = now
                    self._emit(session, rule, "cancelled", now)
                    session.commit()
                    result["cancelled"].append(str(rule.id))
        return result

    def _matcher_sets(self, session, rule):
        from keep.api.models.db.alert import LastAlertToIncident, NULL_FOR_DELETED_AT
        from keep.api.models.db.incident import Incident
        guards = self.team_matchers.get(rule.team_id or "__unassigned__")
        if not guards:
            raise UnsupportedSelector("team_scope_unconfigured")
        selector = rule.selector
        if selector["kind"] == "filter":
            return [filter_matchers(selector["cel"]) + [label_matcher(m["name"], m["value"]) for m in branch]
                    for branch in guards]
        if selector["kind"] == "incident":
            incidents = session.exec(select(Incident).where(Incident.tenant_id == self.tenant_id,
                Incident.id.in_([UUID(str(value)) for value in selector["incident_ids"]]))).all()
            if any(row.team_id != rule.team_id for row in incidents):
                raise UnsupportedSelector("ownership_changed")
            fingerprints = session.exec(select(LastAlertToIncident.fingerprint).where(
                LastAlertToIncident.tenant_id == self.tenant_id, LastAlertToIncident.incident_id.in_([row.id for row in incidents]),
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT)).all()
        else:
            fingerprints = selector["fingerprints"]
        owners = session.exec(select(Alert.team_id).where(Alert.tenant_id == self.tenant_id,
            Alert.fingerprint.in_(fingerprints)).distinct()).all()
        if any(team != rule.team_id for team in owners):
            raise UnsupportedSelector("foreign_alert_history")
        rows = session.exec(select(Alert).join(LastAlert, LastAlert.alert_id == Alert.id).where(
            LastAlert.tenant_id == self.tenant_id, Alert.tenant_id == self.tenant_id,
            LastAlert.fingerprint.in_(fingerprints), Alert.team_id == rule.team_id)).all()
        result = []
        for row in rows:
            labels = (row.event or {}).get("labels", {})
            if not labels or not labels.get("alertname") or any(not isinstance(value, str) or not value for value in labels.values()):
                raise UnsupportedSelector("source_labels_unavailable")
            if not any(all(labels.get(m["name"]) == m["value"] for m in branch) for branch in guards):
                raise UnsupportedSelector("source_scope_mismatch")
            result.append([label_matcher(name, value) for name, value in sorted(labels.items())])
        if not result:
            raise UnsupportedSelector("no_source_targets")
        return result

    def _save_context(self, session, rule, context):
        session.refresh(rule, with_for_update=True)
        rule.external_context = {**(rule.external_context or {}), self.source_id: copy.deepcopy(context)}
        session.add(rule)
        self._fence(session)
        session.commit()

    def _delete(self, session, identifier):
        self._renew(session)
        response = self.http_client.delete(self.alertmanager_url + "/api/v2/silence/" + quote(identifier, safe=""), timeout=10)
        self._renew(session)
        return response.status_code in {200, 404}

    def _retire(self, session, entry):
        identifiers = set(entry.get("retired_ids", []))
        if entry.get("external_id"):
            identifiers.add(entry["external_id"])
        remaining = []
        for identifier in identifiers:
            try:
                if not self._delete(session, identifier):
                    remaining.append(identifier)
            except requests.RequestException:
                remaining.append(identifier)
        entry["retired_ids"] = remaining
        if remaining or entry.get("state") == "uncertain" and not entry.get("external_id"):
            return False
        entry["state"] = "deleted"
        return True

    @serialized
    def sync_keep_silences_to_am(self, session: Session, am_silences: list[dict[str, Any]]) -> dict[str, Any]:
        """Only server-owned receipts authorize an external write or cancellation."""
        now = self.clock()
        result = {"pushed": [], "deleted": [], "unsilenced_from_am": [], "local_only": []}
        if not isinstance(am_silences, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("id"), str) or not isinstance(row.get("status"), dict) for row in am_silences):
            raise ValueError("Invalid Alertmanager silence snapshot")
        remote_by_id = {row["id"]: row for row in am_silences}
        hints = session.exec(select(Silence.id).where(Silence.tenant_id == self.tenant_id,
            Silence.origin != "alertmanager")).all()
        for identifier in hints:
            self._renew(session)
            rule = session.exec(select(Silence).where(Silence.id == identifier).with_for_update()
                .execution_options(populate_existing=True)).one()
            context = copy.deepcopy((rule.external_context or {}).get(self.source_id, {}))
            entries = context.setdefault("entries", {})
            owner_changed = bool(context) and "team_id" in context and context["team_id"] != rule.team_id
            context["team_id"] = rule.team_id
            # Recover a successful create whose response/DB acknowledgement was lost.
            for entry in entries.values():
                tag = " [keep-sync:" + entry["nonce"] + "]"
                matches = [row for row in am_silences if row.get("createdBy") == "keep" and row.get("comment", "").endswith(tag)
                           and row.get("status", {}).get("state") in {"active", "pending"}]
                if len(matches) == 1 and entry.get("state") == "uncertain":
                    found = matches[0]
                    # AM clamps a past start to its current time. This narrows
                    # the interval; it must not prevent recovering our create.
                    requested_start = entry.get("pending_starts_at", found["startsAt"])
                    recovered = {**found, "startsAt": requested_start}
                    if (am_wire_time(found["startsAt"]) >= am_wire_time(requested_start)
                            and (am_time(requested_start) <= now or am_wire_time(found["startsAt"]) == am_wire_time(requested_start))
                            and payload_digest(recovered) == entry.get("pending_digest")):
                        old_id = entry.get("external_id")
                        entry.update(external_id=found["id"], state="synced", digest=entry["pending_digest"])
                        if old_id and old_id != found["id"]:
                            entry.setdefault("retired_ids", []).append(old_id)
                elif len(matches) > 1:
                    # All copies carry the private, persisted nonce. Track every
                    # copy so cancellation cannot leave an orphan suppression.
                    entry["retired_ids"] = list({row["id"] for row in matches} - {entry.get("external_id")})
                    context.update(state="uncertain", reason="duplicate_external_rules")
            stale = rule.cancelled_at is not None or rule.ends_at is not None and rule.ends_at <= now
            if not stale and entries and context.get("synced_revision") == rule.revision:
                confirmed = [entry for entry in entries.values() if entry.get("external_id") and entry.get("state") == "synced"]
                if confirmed and any(entry["external_id"] not in remote_by_id or
                    remote_by_id[entry["external_id"]].get("status", {}).get("state") not in {"active", "pending"}
                    for entry in confirmed):
                    touched = datetime.fromisoformat(context.get("synced_at", now.isoformat()))
                    if (now - touched).total_seconds() >= 30:
                        rule.cancelled_at = now
                        self._emit(session, rule, "cancelled", now)
                        session.commit()
                        stale = True
                        result["unsilenced_from_am"].append(str(rule.id))
            if stale:
                for entry in entries.values():
                    if entry.get("state") != "deleted" and self._retire(session, entry):
                        result["deleted"].append({"keep_id": str(rule.id), "am_id": entry.get("external_id")})
                context["state"] = "deleted" if all(e.get("state") == "deleted" for e in entries.values()) else "uncertain"
                context["reason"] = None if context["state"] == "deleted" else "cancellation_unavailable"
                self._save_context(session, rule, context)
                continue
            try:
                if owner_changed:
                    raise UnsupportedSelector("ownership_changed")
                if not entries and (rule.correlation_id or "").startswith("am_synced:"):
                    raise UnsupportedSelector("legacy_binding_requires_review")
                if rule.ends_at is None:
                    raise UnsupportedSelector("indefinite_silence")
                desired = {digest(sorted(branch, key=lambda m: (m["name"], m["value"]))): branch
                           for branch in self._matcher_sets(session, rule)}
            except UnsupportedSelector as error:
                desired = {}
                context.update(state="local_only", reason=str(error))
                result["local_only"].append({"keep_id": str(rule.id), "reason": str(error)})
            # A narrowed/unsupported selector must retire its old external coverage.
            cleanup_failed = False
            for key, entry in list(entries.items()):
                if key not in desired:
                    if self._retire(session, entry):
                        del entries[key]
                    else:
                        cleanup_failed = True
            if cleanup_failed:
                context.update(state="uncertain", reason="cancellation_unavailable")
                self._save_context(session, rule, context)
                continue
            planned_revision = rule.revision
            for key, matchers in desired.items():
                entry = entries.setdefault(key, {"nonce": uuid4().hex, "state": "pending"})
                comment = (rule.comment or "Mirrored from Keep") + " [keep-sync:" + entry["nonce"] + "]"
                payload = {"matchers": matchers, "startsAt": utc_string(rule.starts_at), "endsAt": utc_string(rule.ends_at),
                           "createdBy": "keep", "comment": comment}
                target_digest = payload_digest(payload)
                if entry.get("digest") == target_digest and entry.get("state") == "synced":
                    continue
                if entry.get("state") == "uncertain":
                    continue  # An ambiguous create never gets a second blind POST.
                if entry.get("external_id"):
                    payload["id"] = entry["external_id"]
                entry.update(state="uncertain", pending_digest=target_digest, pending_starts_at=payload["startsAt"])
                known = remote_by_id.get(entry.get("external_id"))
                if known and rule.starts_at <= now and known.get("status", {}).get("state") == "active":
                    # Preserve AM's actual start when extending an active rule,
                    # so its historic interval can be updated under the same ID.
                    payload["startsAt"] = known["startsAt"]
                self._save_context(session, rule, context)  # Durable intent before the HTTP side effect.
                self._renew(session)
                try:
                    response = self.http_client.post(self.alertmanager_url + "/api/v2/silences", json=payload, timeout=10)
                    self._renew(session)
                    if response.status_code not in {200, 201}:
                        if 400 <= response.status_code < 500:
                            entry["state"] = "pending"
                        continue
                    external_id = response.json().get("silenceID")
                    if not isinstance(external_id, str) or not external_id:
                        continue
                    previous_id = entry.get("external_id")
                    entry.update(external_id=external_id, state="synced", digest=target_digest)
                    if previous_id and previous_id != external_id:
                        # AM may replace the ID when matchers change; retain cleanup responsibility.
                        entry.setdefault("retired_ids", []).append(previous_id)
                    result["pushed"].append({"keep_id": str(rule.id), "am_id": external_id})
                except requests.ConnectTimeout:
                    entry["state"] = "pending"  # Connection was not established.
                except (requests.RequestException, ValueError):
                    pass  # Next successful inventory can confirm the durable nonce.
                finally:
                    self._save_context(session, rule, context)
            if desired:
                done = all(entry.get("state") == "synced" for entry in entries.values())
                context.update(state="synced" if done else "uncertain", reason=None if done else "external_result_unknown")
                if done:
                    if context.get("synced_revision") != planned_revision:
                        context["synced_at"] = now.isoformat()
                    context["synced_revision"] = planned_revision
                    for entry in entries.values():
                        # Successful updates can replace IDs in Alertmanager.
                        for old_id in list(entry.get("retired_ids", [])):
                            try:
                                if self._delete(session, old_id):
                                    entry["retired_ids"].remove(old_id)
                            except requests.RequestException:
                                pass
                        if entry.get("retired_ids"):
                            context.update(state="uncertain", reason="cancellation_unavailable")
            self._save_context(session, rule, context)
        return result

    @serialized
    def _reconcile_once(self, session):
        state = session.get(AlertmanagerReconciliationState, (self.tenant_id, self.source_id))
        if state.next_run_at and state.next_run_at > utc_now():
            return {"skipped": "poll_interval"}
        saved = state.alert_state or {}
        self.last_seen_am_count = saved.get("last_count")
        self.consecutive_misses = saved.get("misses", {})
        self.breaker_since = datetime.fromisoformat(saved["breaker_since"]) if saved.get("breaker_since") else None
        self.breaker_fingerprints = saved.get("breaker_fingerprints")
        try:
            am_alerts, am_silences = self.fetch_alerts(), self.fetch_silences()
            if not isinstance(am_alerts, list) or not isinstance(am_silences, list):
                raise ValueError("Invalid Alertmanager snapshot")
            result = {"alerts": self.reconcile_alerts(session, am_alerts),
                      "silences": self.reconcile_silences(session, am_silences),
                      "keep_to_am": self.sync_keep_silences_to_am(session, am_silences)}
        except (requests.RequestException, ValueError):
            session.rollback()
            self.consecutive_misses.clear()
            self.breaker_since, self.breaker_fingerprints = None, None
            result = {"error": "alertmanager_unavailable", "alerts": {}, "silences": {}}
        state = session.get(AlertmanagerReconciliationState, (self.tenant_id, self.source_id))
        state.alert_state = {"last_count": self.last_seen_am_count, "misses": self.consecutive_misses,
                            "breaker_since": self.breaker_since.isoformat() if self.breaker_since else None,
                            "breaker_fingerprints": self.breaker_fingerprints}
        state.next_run_at = utc_now() + timedelta(seconds=self.interval_seconds)
        session.add(state)
        self._fence(session)
        session.commit()
        return result

    def reconcile_once(self, session: Session | None = None) -> dict[str, Any]:
        from keep.api.core import db
        if session is not None:
            return self._reconcile_once(session)
        with Session(db.engine) as session:
            return self._reconcile_once(session)
