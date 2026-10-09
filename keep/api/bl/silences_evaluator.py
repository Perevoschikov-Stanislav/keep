"""One evaluation snapshot, shared by API, DTOs and database filtering."""

import json
import logging
from collections import defaultdict
from datetime import datetime

import celpy
from fastapi import HTTPException
from pydantic import ValidationError, parse_obj_as
from sqlalchemy import and_, or_
from sqlmodel import select

from keep.api.bl.silences_bl import (
    check_alert_access, check_incident_access, compile_filter, fail,
)
from keep.api.models.alert import AlertSeverity
from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
from keep.api.models.db.helpers import NULL_FOR_DELETED_AT
from keep.api.models.db.incident import Incident
from keep.api.models.db.silence import Silence
from keep.api.models.silence import (
    AlertTarget, EffectiveSilenceItem, EffectiveSilenceResponse, IncidentTarget,
    SilenceMetadata, SilenceReason, utc_now, utc_string,
    Selector,
)

logger = logging.getLogger(__name__)


def chunks(values, size=1000):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def latest_alerts(session, tenant_id, fingerprints=None):
    query = select(Alert).join(LastAlert, and_(
        Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id,
    )).where(LastAlert.tenant_id == tenant_id)
    if fingerprints is None:
        return session.exec(query).all()
    return [row for batch in chunks(fingerprints)
            for row in session.exec(query.where(LastAlert.fingerprint.in_(batch))).all()]


def read_snapshot(session):
    """Keep rules, targets and coverage in one DB snapshot for normal read requests."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
    elif not session.in_transaction() and dialect in {"postgresql", "mysql"}:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _deadline(reasons):
    if any(reason.ends_at is None for reason in reasons):
        return None
    return max(datetime.fromisoformat(reason.ends_at[:-1]) for reason in reasons)


def _unique_reasons(reasons):
    unique = {(str(reason.silence_id), reason.via, str(reason.incident_id)): reason for reason in reasons}
    return [unique[key] for key in sorted(unique)]


class SilenceEvaluator:
    def __init__(self, session, tenant_id, now=None):
        self.session = session
        self.tenant_id = tenant_id
        self.now = now or utc_now()
        read_snapshot(session)
        self.rules = session.exec(select(Silence).where(
            Silence.tenant_id == tenant_id, Silence.cancelled_at.is_(None),
            Silence.starts_at <= self.now,
            or_(Silence.ends_at.is_(None), Silence.ends_at > self.now),
        )).all()
        try:
            for rule in self.rules:
                parse_obj_as(Selector, rule.selector)
        except ValidationError:
            fail(503, "silence_verification_unavailable", "Stored silence cannot be evaluated")
        self.programs = {}
        self.activations = {}

    def _reason(self, rule, via, incident_id=None):
        return SilenceReason(silence_id=rule.id, revision=rule.revision, via=via,
                             incident_id=incident_id, ends_at=utc_string(rule.ends_at),
                             read_only=rule.origin == "alertmanager")

    def _filter_matches(self, rule, alert):
        from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts

        if rule.id not in self.programs:
            try:
                self.programs[rule.id] = compile_filter(rule.selector["cel"], alertmanager=rule.origin == "alertmanager")
            except HTTPException:
                fail(503, "silence_verification_unavailable", "Stored silence cannot be evaluated")
        if alert.id not in self.activations:
            dtos = convert_db_alerts_to_dto_alerts([alert], session=self.session,
                with_silences=False, exclude_silence_fields=True)
            if not dtos:
                return False
            payload = json.loads(dtos[0].json())
            for key in ("dismissed", "dismissUntil", "silence"):
                payload.pop(key, None)
            if isinstance(payload.get("severity"), str):
                try:
                    payload["severity"] = AlertSeverity(payload["severity"].lower()).order
                except ValueError:
                    pass
            self.activations[alert.id] = celpy.json_to_cel(payload)
        try:
            value = self.programs[rule.id].evaluate(self.activations[alert.id])
            if isinstance(value, (bool, celpy.celtypes.BoolType)):
                return bool(value)
        except (celpy.CELEvalError, TypeError, ValueError, RecursionError):
            pass
        logger.debug("Silence CEL did not produce a boolean", extra={"silence_id": str(rule.id)})
        return False

    def alerts(self, alerts):
        """Evaluate canonical events, including historical events supplied by a trusted caller."""
        parents = defaultdict(list)
        if any(rule.selector["kind"] == "incident" for rule in self.rules):
            for batch in chunks({alert.fingerprint for alert in alerts}):
                rows = self.session.exec(select(
                    LastAlertToIncident.fingerprint, Incident,
                ).join(Incident, and_(
                    Incident.id == LastAlertToIncident.incident_id,
                    Incident.tenant_id == LastAlertToIncident.tenant_id,
                )).where(LastAlertToIncident.tenant_id == self.tenant_id,
                         LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                         LastAlertToIncident.fingerprint.in_(batch))).all()
                for fingerprint, incident in rows:
                    parents[(fingerprint, incident.team_id)].append(incident)
        results = {}
        for alert in alerts:
            reasons = []
            for rule in self.rules:
                if alert.tenant_id != self.tenant_id or rule.team_id != alert.team_id:
                    continue
                selector = rule.selector
                if selector["kind"] == "alert" and alert.fingerprint in selector["fingerprints"]:
                    reasons.append(self._reason(rule, "fingerprint"))
                elif selector["kind"] == "filter" and self._filter_matches(rule, alert):
                    reasons.append(self._reason(rule, "filter"))
                elif selector["kind"] == "incident":
                    from keep.api.core import incident_lifecycle as life
                    for incident in parents[(alert.fingerprint, alert.team_id)]:
                        if str(incident.id) not in selector["incident_ids"]:
                            continue
                        # Keep the explicit old-ID rule active, including its
                        # resolved notification. A later lifecycle episode of
                        # this fingerprint has its own scope.
                        policy = life.policy_for(incident)
                        resolved_at = (incident.lifecycle_context or {}).get("resolved_at")
                        if policy and resolved_at and incident.status == "resolved":
                            at = life.event_time(alert, policy)
                            if at is not None and at > life.utc(resolved_at):
                                continue
                        reasons.append(self._reason(rule, "incident", incident.id))
            reasons = _unique_reasons(reasons)
            silenced = bool(reasons)
            results[alert.id] = EffectiveSilenceItem(
                target=AlertTarget(kind="alert", fingerprint=alert.fingerprint),
                silenced=silenced, coverage="full" if silenced else "none",
                silenced_until=utc_string(_deadline(reasons)) if silenced else None,
                reasons=reasons, total_alerts=1, silenced_alerts=int(silenced),
            )
        return results

    def incidents(self, incidents):
        links = defaultdict(set)
        for batch in chunks({incident.id for incident in incidents}):
            for incident_id, fingerprint in self.session.exec(select(
                LastAlertToIncident.incident_id, LastAlertToIncident.fingerprint,
            ).where(LastAlertToIncident.tenant_id == self.tenant_id,
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    LastAlertToIncident.incident_id.in_(batch))).all():
                links[incident_id].add(fingerprint)
        alerts = latest_alerts(self.session, self.tenant_id, set().union(*links.values()) if links else set())
        # Canonical status/deleted enrichments affect incident coverage, just as in the alert list.
        from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts

        dtos = convert_db_alerts_to_dto_alerts(alerts, session=self.session,
            with_silences=False, exclude_silence_fields=True)
        dto_by_id = {dto.event_id: dto for dto in dtos}
        by_fingerprint = {alert.fingerprint: alert for alert in alerts}
        alert_results = self.alerts(alerts)
        results = {}
        for incident in incidents:
            relevant = []
            for fingerprint in links[incident.id]:
                alert = by_fingerprint.get(fingerprint)
                dto = dto_by_id.get(str(alert.id)) if alert else None
                if dto is not None and not dto.deleted and (
                    incident.status == "resolved" or dto.status in {"firing", "acknowledged"}
                ):
                    relevant.append(alert_results[alert.id])
            explicit = [self._reason(rule, "incident", incident.id) for rule in self.rules
                        if rule.team_id == incident.team_id and rule.selector["kind"] == "incident"
                        and str(incident.id) in rule.selector["incident_ids"]]
            count = sum(item.silenced for item in relevant)
            silenced = bool(explicit) or bool(relevant) and count == len(relevant)
            until = None
            if silenced:
                if explicit:
                    until = _deadline(explicit)
                if relevant and count == len(relevant):
                    deadlines = [datetime.fromisoformat(item.silenced_until[:-1])
                                 for item in relevant if item.silenced_until is not None]
                    coverage_end = min(deadlines) if deadlines else None
                    if not explicit:
                        until = coverage_end
                    elif until is not None:
                        until = max(until, coverage_end) if coverage_end is not None else None
            results[incident.id] = EffectiveSilenceItem(
                target=IncidentTarget(kind="incident", incident_id=incident.id), silenced=silenced,
                coverage="full" if silenced else "partial" if count else "none",
                silenced_until=utc_string(until),
                reasons=_unique_reasons(explicit + [reason for item in relevant for reason in item.reasons]),
                total_alerts=len(relevant), silenced_alerts=len(relevant) if explicit else count,
            )
        return results

    def effective(self, entity, targets):
        # Authorize the whole batch before constructing any response.
        incidents = []
        fingerprints = []
        for target in targets:
            if target.kind == "alert":
                check_alert_access(self.session, entity, target.fingerprint)
                fingerprints.append(target.fingerprint)
            else:
                incidents.append(check_incident_access(self.session, entity, target.incident_id))
        alerts = latest_alerts(self.session, self.tenant_id, fingerprints)
        found = {alert.fingerprint: alert for alert in alerts}
        if any(fingerprint not in found for fingerprint in fingerprints):
            fail(404, "not_found", "Not found")
        alert_results = self.alerts(alerts)
        incident_results = self.incidents(incidents)
        return EffectiveSilenceResponse(evaluated_at=utc_string(self.now), items=[
            alert_results[found[target.fingerprint].id] if target.kind == "alert"
            else incident_results[target.incident_id] for target in targets
        ])

    def metadata(self, item):
        return SilenceMetadata(evaluated_at=utc_string(self.now), **item.dict(exclude={"target"}))
