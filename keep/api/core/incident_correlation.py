"""IaC correlation owns grouping and evidence; notification transports do not."""

import copy
import hashlib
import json
import math
from datetime import timedelta
from uuid import UUID

import celpy
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from keep.api.core.incident_contract import require
from keep.api.core.event_normalization import known_normalized_field, refresh_incident_presentation
from keep.api.models.alert import AlertSeverity, AlertStatus
from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident, NULL_FOR_DELETED_AT
from keep.api.models.db.incident import Incident, IncidentStatus, IncidentType
from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
from keep.api.models.db.tenant import Tenant
from keep.api.models.incident import IncidentDto

MISSING = object()


def value_at(payload, path):
    for part in path.split("."):
        if not isinstance(payload, dict) or part not in payload:
            return MISSING
        payload = payload[part]
    return payload


def typed(value):
    if value is MISSING:
        return ["missing"]
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite grouping value")
        return ["float", value]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["list", [typed(item) for item in value]]
    if isinstance(value, dict):
        return ["map", [[key, typed(value[key])] for key in sorted(value)]]
    raise ValueError("unsupported grouping value")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_correlation(bundle):
    require(not bundle.get("correlation") or not bundle.get("rules"), "correlation", "choose IaC correlation or legacy rules")
    for rule in bundle.get("correlation", []):
        require(not rule.get("multi_level") or (len(rule["group_by"]) == 1 and rule.get("multi_level_property_name")),
                "correlation.multi_level", "one dictionary group_by and property name required")
        require(rule.get("multi_level") or not rule.get("multi_level_property_name"),
                "correlation.multi_level_property_name", "requires multi_level")
        if rule["create_on"] == "all":
            environment = celpy.Environment()
            tree = environment.compile(rule["match"])
            require(not any(len(node.children) == 3 for node in tree.find_data("expr")),
                    "correlation.create_on", "all requires boolean OR branches, not conditional expressions")
            for expression in branches(rule["match"]):
                environment.compile(expression)


def grouping_keys(tenant_id, event, rule, revision):
    payload, invalid = event.dict(), []
    for path in rule["required_fields"]:
        value = value_at(payload, path)
        reason = "missing" if value is MISSING else "null" if value is None else "empty" if value in ("", [], {}) else None
        if not reason and not known_normalized_field(payload, path):
            reason = "unknown_normalized"
        try:
            typed(value)
        except ValueError:
            reason = "invalid_type"
        if reason:
            invalid.append({"path": path, "state": reason})
    values = [[path, typed(value_at(payload, path))] for path in rule["group_by"]] if not invalid else []
    groups = [values]
    if not invalid and rule.get("multi_level"):
        container = value_at(payload, rule["group_by"][0])
        if not isinstance(container, dict):
            invalid.append({"path": rule["group_by"][0], "state": "invalid_multilevel"})
        else:
            groups = []
            for name in sorted(container):
                value = value_at(container[name], rule["multi_level_property_name"])
                if value is MISSING or value is None or value in ("", [], {}):
                    invalid.append({"path": rule["group_by"][0] + "." + name, "state": "missing_multilevel_value"})
                else:
                    groups.append([[rule["group_by"][0] + ".*." + rule["multi_level_property_name"], typed(value)]])
    fallback = bool(invalid)
    if invalid:
        if rule["missing_required"] == "skip_correlation":
            return [], invalid
        groups = [[["alert.fingerprint", typed(event.fingerprint)]]]
    result = {}
    for values in groups:
        key = digest(["correlation-v1", tenant_id, event.team_id, rule["id"], revision, values])
        result[key] = {"key": key, "values": values, "fallback": fallback}
    return list(result.values()), invalid


def branches(expression):
    """Split top-level OR, keeping nested AND, strings and method arguments intact."""
    expression = expression.strip()
    depth, quoted, escaped, offsets = 0, None, False, []
    outer_end = None
    for index, char in enumerate(expression):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quoted:
                quoted = None
            continue
        if char in ("'", '"'):
            quoted = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0 and outer_end is None:
                outer_end = index
        elif depth == 0 and expression[index:index + 2] == "||":
            offsets.append(index)
    if expression.startswith("(") and outer_end == len(expression) - 1:
        return branches(expression[1:-1])
    result, start = [], 0
    for offset in offsets:
        result.append(expression[start:offset].strip())
        start = offset + 2
    return result + [expression[start:].strip()]


def matches(expression, payload):
    environment = celpy.Environment()
    result = environment.program(environment.compile(expression)).evaluate(celpy.json_to_cel(payload))
    if not isinstance(result, (bool, celpy.celtypes.BoolType)):
        raise ValueError("CEL predicate must return boolean")
    return bool(result)


def linked_alerts(session, tenant_id, incident):
    return session.exec(select(Alert).join(LastAlert, LastAlert.alert_id == Alert.id).join(
        LastAlertToIncident, (LastAlertToIncident.tenant_id == LastAlert.tenant_id)
        & (LastAlertToIncident.fingerprint == LastAlert.fingerprint)).where(
            Alert.tenant_id == tenant_id, LastAlert.tenant_id == tenant_id,
            LastAlertToIncident.tenant_id == tenant_id, LastAlertToIncident.incident_id == incident.id,
            LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT, Alert.team_id == incident.team_id)
        .order_by(LastAlert.first_timestamp, LastAlert.fingerprint)).all()


def lock_group(session, tenant_id, team_id, rule, revision, key):
    # Savepoint contains only insert; a concurrent insert waits, then the winner's
    # row is locked. Every later mutation stays in this same outer transaction.
    values = dict(id=key, tenant_id=tenant_id, team_id=team_id, rule_id=rule["id"], rule_version=revision)
    dialect = session.get_bind().dialect.name
    if dialect in {"postgresql", "sqlite"}:
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        session.execute(insert(IncidentCorrelationGroup).values(**values).on_conflict_do_nothing(index_elements=["id"]))
    elif session.get(IncidentCorrelationGroup, key) is None:
        try:
            with session.begin_nested():
                session.add(IncidentCorrelationGroup(**values))
                session.flush()
        except IntegrityError:
            pass
    return session.exec(select(IncidentCorrelationGroup).where(IncidentCorrelationGroup.id == key)
        .with_for_update().execution_options(populate_existing=True)).one()


def _join(session, tenant_id, event, row, rule, lifecycle, revision, group_key, snapshot, *, allow_create=True):
    from keep.api.core import incident_lifecycle as life
    group = lock_group(session, tenant_id, row.team_id, rule, revision, group_key["key"])
    incident = session.exec(select(Incident).where(Incident.id == group.incident_id, Incident.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True)).first() if group.incident_id else None
    at = life.event_time(row, lifecycle)
    if at is None:
        return None, None, "invalid_event_time"
    state = copy.deepcopy(group.lifecycle_state or {"members": {}, "transitions": []})
    watermark = life.utc(state["watermark"]) if state.get("watermark") else group.opened_at
    cursor = state["members"].get(row.fingerprint)
    if watermark and at < watermark or (cursor and at == life.utc(cursor["at"]) and row.event["status"] != cursor["status"]):
        return None, None, "history_only"
    reopening = False
    if incident and incident.team_id != row.team_id:
        previous, incident = None, None
        state = {"members": {}, "transitions": []}
        group.lifecycle_state = None
    elif incident and incident.status == "resolved" and row.event["status"] == "firing" and allow_create:
        reopen = lifecycle["reopen"]
        resolved_at = (incident.lifecycle_context or {}).get("resolved_at") or incident.end_time
        reopening = (reopen["mode"] == "reopen" and resolved_at is not None
            and life.utc(resolved_at) <= at < life.utc(resolved_at) + timedelta(seconds=reopen["within_seconds"]))
        previous = None if reopening else incident
        if not reopening:
            incident = None
    elif incident and (incident.status in {"merged", "deleted"}
                      or (allow_create and row.event["status"] == "firing"
                          and at >= group.opened_at + timedelta(seconds=rule["window_seconds"]))):
        previous, incident = incident, None
    else:
        previous = None
    created = incident is None
    if incident is None:
        if row.event["status"] != AlertStatus.FIRING.value or not allow_create:
            return None, None, "not_firing"
        incident = Incident(tenant_id=tenant_id, team_id=row.team_id, generated_name=rule["id"],
            rule_fingerprint=group.id, incident_type=IncidentType.RULE.value,
            resolve_on=lifecycle["resolve_on"], is_predicted=True, is_visible=False,
            same_incident_in_the_past_id=previous.id if previous else None,
            assignee=previous.assignee if previous and lifecycle["reopen"]["assignee"] == "preserve" else None,
            start_time=at, last_seen_time=at,
            correlation_context={"team_id": row.team_id, "rule_id": rule["id"], "rule_version": revision,
                "config_digest": snapshot["digest"], "policy": copy.deepcopy(rule), "lifecycle": copy.deepcopy(lifecycle),
                "overlap": snapshot["bundle"]["correlation_overlap"], "group_values": group_key["values"],
                "fallback": group_key["fallback"], "window_start": at.isoformat(),
                "window_end": (at + timedelta(seconds=rule["window_seconds"])).isoformat()})
        session.add(incident)
        session.flush()
        state["members"] = {}
        group.incident_id, group.opened_at = incident.id, at
        session.add(group)
    link = session.exec(select(LastAlertToIncident).where(LastAlertToIncident.tenant_id == tenant_id,
        LastAlertToIncident.incident_id == incident.id, LastAlertToIncident.fingerprint == row.fingerprint)
        .order_by(LastAlertToIncident.deleted_at)).first()
    if link and link.deleted_at != NULL_FOR_DELETED_AT:
        return incident, None, "manually_unlinked"
    if not link:
        session.add(LastAlertToIncident(tenant_id=tenant_id, incident_id=incident.id, fingerprint=row.fingerprint))
        session.flush()
    state["watermark"] = at.isoformat()
    first_at = state["members"].get(row.fingerprint, {}).get("first_at", at.isoformat())
    old_cursor = state["members"].get(row.fingerprint)
    state["members"][row.fingerprint] = {"id": str(row.id), "at": at.isoformat(), "first_at": first_at, "status": row.event["status"],
        "episode": old_cursor.get("episode", 1) if old_cursor else 1}
    group.lifecycle_state = state
    life.initialize(incident, group, lifecycle, at)
    if created:
        state = copy.deepcopy(group.lifecycle_state)
        context = copy.deepcopy(incident.lifecycle_context)
        context["flapping"] = life.flap(state, lifecycle, at, "firing")
        incident.lifecycle_context, group.lifecycle_state = context, state
    elif reopening:
        target = "acknowledged" if lifecycle["reopen"]["ack"] == "preserve" and incident.lifecycle_context.get("ack_by") else "firing"
        if lifecycle["reopen"]["assignee"] == "clear":
            incident.assignee = None
        life.transition(session, incident, target, at=at, reason="reoccurrence within reopen window",
                        group=group, received_at=row.timestamp, reoccurrence=True)
        group.opened_at = at
    state = copy.deepcopy(group.lifecycle_state)
    if row.event["status"] in IncidentStatus.get_active(True) or not old_cursor or old_cursor["status"] in IncidentStatus.get_active(True):
        state["members"][row.fingerprint]["episode"] = incident.lifecycle_context["episode"]
    group.lifecycle_state = state
    from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
    members = life.accepted_members(session, incident, group)
    canonical = convert_db_alerts_to_dto_alerts(members, with_silences=False, session=session)
    was_visible = incident.is_visible
    incident.alerts_count = len(members)
    times = [life.event_time(member, lifecycle) for member in members]
    incident.last_seen_time = max(times)
    incident.start_time = min(times)
    incident.sources = sorted({source for member in canonical for source in member.source})
    if not incident.forced_severity:
        incident.severity = max(AlertSeverity(member.severity).order for member in canonical)
    firing = [member for member in canonical if member.status == AlertStatus.FIRING.value]
    if not incident.is_visible and len(firing) >= rule["threshold"]:
        expressions = branches(rule["match"]) if rule["create_on"] == "all" else [rule["match"]]
        covered = set()
        for member in firing:
            for index, expression in enumerate(expressions):
                try:
                    if matches(expression, member.dict()):
                        covered.add(index)
                except Exception:
                    continue
        incident.is_visible = len(covered) == len(expressions)
    life.update_members(session, incident, group, lifecycle, at, canonical)
    refresh_incident_presentation(tenant_id, incident, session, events=[member.event for member in members])
    from keep.api.core.incident_automation import sync_incident
    sync_incident(session, incident, now=at, snapshot=snapshot)
    session.add(incident)
    event_type = "created" if incident.is_visible and not was_visible else "updated" if incident.is_visible else None
    if event_type:
        from keep.api.core.incident_notifications import record_event
        kind = "incident.created" if event_type == "created" else (
            "alert.recovered" if old_cursor and old_cursor["status"] != "resolved" and row.event["status"] == "resolved" else
            "alert.added" if not old_cursor or old_cursor["status"] == "resolved" and row.event["status"] == "firing" else
            "incident.repeated" if row.event["status"] == "firing" else "incident.updated")
        record_event(session, incident, kind, at, source_id=row.id,
                     count=len(firing) if kind == "incident.created" else 1,
                     objects=[(event.normalized or {}).get("resource") or event.name])
    return incident, event_type, "separate_alert" if group_key["fallback"] else "matched"


def correlate_event(tenant_id, event, snapshot, session):
    """A saved alert version is correlated once; its evidence is immutable."""
    from keep.api.core.incident_runtime_ownership import gate_reason
    reason = gate_reason(snapshot, event.team_id, "domain")
    if reason:
        event.correlation = {"decisions": [{"reason": reason}]}
        return []
    identifier = getattr(event, "event_id", None) or event.id
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError):
        event.correlation = {"decisions": [{"reason": "unsaved_event"}]}
        return []
    row = session.exec(select(Alert).where(Alert.id == identifier, Alert.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True)).first()
    if row is None or row.fingerprint != event.fingerprint or row.team_id != event.team_id:
        event.correlation = {"decisions": [{"reason": "ownership_mismatch"}]}
        return []
    if row.correlation_context is not None:
        event.correlation = copy.deepcopy(row.correlation_context)
        session.commit()
        return []
    # Lifecycle admission reads all relevant cursors after the common lock, so
    # a late event cannot escape its old cursor by changing the grouping fields.
    session.exec(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True)).one()
    linked = session.exec(select(Incident).join(LastAlertToIncident,
        LastAlertToIncident.incident_id == Incident.id).where(Incident.tenant_id == tenant_id,
        Incident.team_id == row.team_id, LastAlertToIncident.tenant_id == tenant_id,
        LastAlertToIncident.fingerprint == row.fingerprint,
        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT)).all()
    from keep.api.core import incident_lifecycle as life
    source_cursors = []
    for incident in linked:
        context = incident.correlation_context
        group = session.get(IncidentCorrelationGroup, incident.rule_fingerprint) if context else None
        if group and group.team_id == row.team_id and context.get("team_id") == row.team_id and context["lifecycle"]["clock"] == "event_time":
            cursor = (group.lifecycle_state or {}).get("members", {}).get(row.fingerprint)
            if cursor:
                source_cursors.append(cursor)
    from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
    canonical_event = convert_db_alerts_to_dto_alerts([row], with_silences=False, session=session)[0]
    decisions, work = [], []
    latest = session.get(LastAlert, (tenant_id, row.fingerprint))
    owner = session.get(Alert, latest.alert_id) if latest else None
    if owner and owner.team_id != row.team_id:
        decisions.append({"reason": "ownership_mismatch"})
    elif latest is None:
        decisions.append({"reason": "history_only"})
    else:
        selected = False
        for rule in sorted(snapshot["bundle"]["correlation"], key=lambda item: item["priority"], reverse=True):
            if row.team_id not in rule["team_ids"]:
                continue
            lifecycle = next(policy for policy in snapshot["bundle"]["lifecycle"] if policy["id"] == rule["lifecycle_ref"])
            revision = digest({"rule": rule, "lifecycle": lifecycle, "overlap": snapshot["bundle"]["correlation_overlap"]})
            decision = {"rule_id": rule["id"], "rule_version": revision, "match": rule["match"], "priority": rule["priority"]}
            if lifecycle["clock"] == "receive_time" and latest.alert_id != row.id:
                decisions.append({**decision, "reason": "history_only"})
                continue
            if lifecycle["clock"] == "event_time":
                at = life.event_time(row, lifecycle)
                if at is not None and any(at < life.utc(cursor["at"]) or
                        (at == life.utc(cursor["at"]) and row.event["status"] != cursor["status"]) for cursor in source_cursors):
                    decisions.append({**decision, "reason": "history_only"})
                    continue
            try:
                matched = matches(rule["match"], canonical_event.dict())
            except Exception:
                decisions.append({**decision, "reason": "match_evaluation_error"})
                continue
            if not matched:
                continue
            if selected and snapshot["bundle"]["correlation_overlap"] == "first_match":
                decisions.append({**decision, "reason": "overlap_lower_priority"})
                continue
            selected = True
            keys, invalid = grouping_keys(tenant_id, canonical_event, rule, revision)
            decision["missing_fields"] = invalid
            if not keys:
                decisions.append({**decision, "reason": "missing_required"})
            for key in keys:
                evidence = {**decision, "group_key": key["key"], "group_values": key["values"]}
                decisions.append(evidence)
                work.append((key["key"], rule, lifecycle, revision, key, evidence, True))
        if not decisions:
            decisions.append({"reason": "no_matching_rule"})
        # Resolution of existing members follows their pinned policy even if
        # the active match expression/configuration no longer selects this alert.
        scheduled = {item[0] for item in work}
        for incident in linked:
            context = incident.correlation_context
            if (incident.status not in IncidentStatus.get_active(True) or not context
                    or context.get("team_id") != row.team_id or incident.rule_fingerprint in scheduled):
                continue
            if context["lifecycle"]["clock"] == "receive_time" and latest.alert_id != row.id:
                continue
            key = {"key": incident.rule_fingerprint, "values": context["group_values"], "fallback": context["fallback"]}
            evidence = {"rule_id": context["rule_id"], "rule_version": context["rule_version"], "group_key": key["key"], "pinned": True}
            decisions.append(evidence)
            work.append((key["key"], context["policy"], context["lifecycle"], context["rule_version"], key, evidence, False))
            scheduled.add(key["key"])
    changed = []
    # Every ingestion locks fanout keys in the same order, independent of priority.
    for _, rule, lifecycle, revision, key, evidence, allow_create in sorted(work, key=lambda item: item[0]):
        incident, event_type, reason = _join(session, tenant_id, canonical_event, row, rule, lifecycle, revision, key, snapshot, allow_create=allow_create)
        evidence["reason"] = reason
        if incident:
            evidence["incident_id"] = str(incident.id)
            if event_type:
                changed.append((incident, event_type))
    row.correlation_context = {"team_id": row.team_id, "config_digest": snapshot["digest"], "decisions": decisions}
    session.add(row)
    session.commit()
    event.correlation = copy.deepcopy(row.correlation_context)
    return [(IncidentDto.from_db_incident(incident, session=session), event_type) for incident, event_type in changed]
