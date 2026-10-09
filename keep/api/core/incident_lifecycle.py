"""Atomic incident transitions and group-level lifecycle state, independent of delivery."""

import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import select

from keep.api.models.action_type import ActionType
from keep.api.models.db.alert import Alert, LastAlertToIncident
from keep.api.models.db.incident import Incident, IncidentStatus
from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
from keep.api.models.db.tenant import Tenant


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def event_time(row, policy):
    if policy["clock"] == "receive_time":
        return utc(row.timestamp)
    try:
        return utc(row.event["lastReceived"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None  # No silent fallback from a provider clock to a receive clock.


def lock_incident(session, tenant_id, incident_id, entity=None):
    """Same lock order as correlation; authorization is rechecked on the locked row."""
    session.exec(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True)).first()
    incident_id = UUID(str(incident_id))
    incident = session.exec(select(Incident).where(Incident.id == incident_id, Incident.tenant_id == tenant_id)).first()
    if incident is None:
        raise HTTPException(404, "Incident not found")
    group = None
    if incident.correlation_context:
        group = session.exec(select(IncidentCorrelationGroup).where(
            IncidentCorrelationGroup.id == incident.rule_fingerprint,
            IncidentCorrelationGroup.tenant_id == tenant_id).with_for_update()
            .execution_options(populate_existing=True)).first()
    incident = session.exec(select(Incident).where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True)).one()
    if entity is not None:
        from keep.identitymanager.rbac import get_role_by_role_name
        from keep.identitymanager.team_access import has_global_access, require_team_access, visible_team_ids
        if entity.tenant_id != tenant_id or not get_role_by_role_name(entity.role).has_scopes(["update:incident"]):
            raise HTTPException(403, "Incident update requires permission")
        if hasattr(entity, "service_scopes") and "update:incident" not in entity.service_scopes:
            raise HTTPException(403, "Service client cannot update incidents")
        require_team_access(entity, incident.team_id, for_write=True)
        if not has_global_access(entity):
            teams = session.exec(select(Alert.team_id).join(LastAlertToIncident,
                (LastAlertToIncident.tenant_id == Alert.tenant_id) & (LastAlertToIncident.fingerprint == Alert.fingerprint))
                .where(LastAlertToIncident.tenant_id == tenant_id, LastAlertToIncident.incident_id == incident.id)
                .distinct()).all()
            if any(team != incident.team_id for team in teams):
                raise HTTPException(403 if visible_team_ids(entity) is None else 404, "Not found")
    if group and (group.incident_id != incident.id or group.team_id != incident.team_id):
        group = None  # An old episode cannot mutate the current episode's counters.
    return incident, group


def policy_for(incident):
    context = incident.correlation_context
    return context["lifecycle"] if context and context.get("team_id") == incident.team_id else None


def phase(status):
    return "firing" if status in IncidentStatus.get_active(True) else "resolved" if status == "resolved" else None


def flap(state, policy, at, new_phase=None):
    config = policy["flapping"]
    previous = state.get("last_transition_at")
    if previous and at >= utc(previous) + timedelta(seconds=config["reset_after_seconds"]):
        state.update(transitions=[], flapping=False, flapping_since=None)
    lower = at - timedelta(seconds=config["window_seconds"])
    state["transitions"] = [value for value in state.get("transitions", []) if utc(value) > lower]
    if new_phase is not None:
        if state.get("phase") is not None and new_phase != state["phase"]:
            state["transitions"].append(at.isoformat())
            state["last_transition_at"] = at.isoformat()
        state["phase"] = new_phase
    if config["enabled"] and len(state["transitions"]) >= config["transition_threshold"] and not state.get("flapping"):
        state.update(flapping=True, flapping_since=at.isoformat())
    if not config["enabled"]:
        state.update(flapping=False, flapping_since=None)
    return {"enabled": config["enabled"], "active": bool(state.get("flapping")),
        "transition_count": len(state["transitions"]), "window_seconds": config["window_seconds"],
        "transition_threshold": config["transition_threshold"], "reset_after_seconds": config["reset_after_seconds"],
        "since": state.get("flapping_since"), "last_transition_at": state.get("last_transition_at")}


def initialize(incident, group, policy, at):
    if group.lifecycle_state is None:
        group.lifecycle_state = {"members": {}, "phase": phase(incident.status), "transitions": []}
    if incident.lifecycle_context is None:
        incident.lifecycle_context = {"team_id": incident.team_id, "group_id": group.id,
            "policy_version": group.rule_version, "clock": policy["clock"], "revision": 0, "episode": 1,
            "episode_start": at.isoformat()}


def transition(session, incident, new_status, *, at, actor="system", reason="manual", group=None, received_at=None,
               reoccurrence=False, expected_revision=None):
    """Caller owns the locks and transaction; audit and state commit together."""
    from keep.api.core.db import add_audit
    at = utc(at)
    policy = policy_for(incident)
    if group and policy:
        initialize(incident, group, policy, at)
    new_status = new_status.value if isinstance(new_status, IncidentStatus) else new_status
    context = copy.deepcopy(incident.lifecycle_context or {"team_id": incident.team_id, "revision": 0,
        "episode": 1, "episode_start": utc(incident.start_time or incident.creation_time).isoformat()})
    if expected_revision is not None and expected_revision != context["revision"]:
        raise HTTPException(409, "Incident changed; refresh before changing status")
    if incident.status in {"merged", "deleted"} and incident.status != new_status:
        raise HTTPException(409, "Closed incident cannot change status")
    if new_status == incident.status and not reoccurrence:
        return False
    old_status = incident.status
    previous_assignee = incident.assignee
    previous_resolution = context.get("resolved_at")
    was_flapping = bool((context.get("flapping") or {}).get("active"))
    if old_status == "resolved" and new_status in IncidentStatus.get_active(True) and not reoccurrence:
        if policy:
            if group is None:
                raise HTTPException(409, "Episode is superseded; use the current incident")
            reopen = policy["reopen"]
            resolved_at = context.get("resolved_at") or incident.end_time
            if (reopen["mode"] != "reopen" or resolved_at is None
                    or not utc(resolved_at) <= at < utc(resolved_at) + timedelta(seconds=reopen["within_seconds"])):
                raise HTTPException(409, "Policy requires a new incident; ingest a new firing event")
            if reopen["assignee"] == "clear":
                incident.assignee = None
        reoccurrence = True
    if reoccurrence:
        context.update(episode=context.get("episode", 1) + 1, episode_start=at.isoformat(), reopened_at=at.isoformat())
    if new_status == "acknowledged" and (not reoccurrence or reason == "manual"):
        context.update(ack_by=actor, ack_at=at.isoformat())
        if actor and actor != "system" and actor != incident.assignee:
            incident.assignee = actor
            add_audit(incident.tenant_id, str(incident.id), actor, ActionType.INCIDENT_ASSIGN,
                      "Incident self-assigned to " + actor, session, commit=False)
    elif new_status == "firing":
        context.pop("ack_by", None)
        context.pop("ack_at", None)
        if old_status == "acknowledged":
            incident.assignee = None
    incident.end_time = at if new_status == "resolved" else None
    if new_status == "resolved":
        context["resolved_at"] = at.isoformat()
    else:
        context.pop("resolved_at", None)
    context.update(revision=context["revision"] + 1, last_status_change_at=at.isoformat(), last_reason=reason)
    if group and policy:
        state = copy.deepcopy(group.lifecycle_state or {"members": {}, "phase": phase(old_status), "transitions": []})
        context["flapping"] = flap(state, policy, at, phase(new_status))
        context["transition_times"] = state["transitions"]
        if reoccurrence:
            group.opened_at = at
            for cursor in state.get("members", {}).values():
                if reason == "manual" or cursor["status"] in IncidentStatus.get_active(True):
                    cursor["episode"] = context["episode"]
        state["watermark"] = max(at, utc(state["watermark"]) if state.get("watermark") else at).isoformat()
        group.lifecycle_state = state
        session.add(group)
    incident.status, incident.lifecycle_context = new_status, context
    from keep.api.core.incident_automation import sync_incident
    from keep.api.core.incident_configuration import active_configuration
    sync_incident(session, incident, now=at, snapshot=active_configuration(incident.tenant_id))
    from keep.api.core.event_normalization import refresh_incident_presentation
    refresh_incident_presentation(incident.tenant_id, incident, session)
    session.add(incident)
    add_audit(incident.tenant_id, str(incident.id), actor, ActionType.INCIDENT_STATUS_CHANGE,
        f"Incident status changed from {old_status} to {new_status}; {reason}; clock={at.isoformat()}; "
        f"received={(utc(received_at) if received_at else at).isoformat()}; revision={context['revision']}; "
        f"policy={context.get('policy_version', 'legacy')}",
        session, commit=False)
    from keep.api.core.incident_notifications import record_event
    event_type = ("incident.reopened" if reoccurrence else "incident.acknowledged" if new_status == "acknowledged" else
                  "incident.unacknowledged" if old_status == "acknowledged" and new_status == "firing" else
                  "incident.resolved" if new_status == "resolved" else "incident.updated")
    record_event(session, incident, event_type, at, actor=actor, previous_status=old_status,
                 elapsed_since_resolution_seconds=max(0, (at - utc(previous_resolution)).total_seconds()) if previous_resolution else 0)
    if previous_assignee != incident.assignee:
        record_event(session, incident, "incident.assignee_changed", at, actor=actor, previous_assignee=previous_assignee)
    if not was_flapping and (context.get("flapping") or {}).get("active"):
        record_event(session, incident, "incident.flapping", at, actor=actor)
    return True


def assign(session, incident, assignee, entity, *, at, expected_revision=None):
    """Assignment shares the locked incident/revision used by UI and integration commands."""
    from keep.api.core.db import add_audit
    from keep.api.models.db.user import User
    from keep.identitymanager.rbac import get_role_by_role_name
    revision = (incident.lifecycle_context or {}).get("revision", 0)
    if expected_revision is not None and expected_revision != revision:
        raise HTTPException(409, "Incident changed; refresh before assignment")
    if incident.status in {"merged", "deleted"}:
        raise HTTPException(409, "Closed incident cannot be assigned")
    if assignee is not None and assignee != entity.email:
        user = session.exec(select(User).where(User.tenant_id == incident.tenant_id, User.username == assignee)).first()
        if entity.role != "admin" or not user or not get_role_by_role_name(user.role).has_scopes(["update:incident"]):
            raise HTTPException(403, "Assignee must be an authorized Keep user")
    if incident.assignee == assignee:
        return False
    previous_assignee = incident.assignee
    incident.assignee = assignee
    context = copy.deepcopy(incident.lifecycle_context or {"team_id": incident.team_id, "episode": 1})
    context["revision"] = revision + 1
    incident.lifecycle_context = context
    session.add(incident)
    add_audit(incident.tenant_id, str(incident.id), entity.email, ActionType.INCIDENT_ASSIGN,
              "Incident assigned to " + (assignee or "unassigned"), session, commit=False)
    from keep.api.core.incident_notifications import record_event
    record_event(session, incident, "incident.assignee_changed", at, actor=entity.email, previous_assignee=previous_assignee)
    return True


def accepted_members(session, incident, group):
    """Accepted provider versions, constrained by current canonical team and manual membership."""
    from keep.api.core.incident_correlation import linked_alerts
    current = linked_alerts(session, incident.tenant_id, incident)
    cursors = (group.lifecycle_state or {}).get("members", {})
    ids = {UUID(cursors[row.fingerprint]["id"]) for row in current if row.fingerprint in cursors}
    accepted = session.exec(select(Alert).where(Alert.tenant_id == incident.tenant_id,
        Alert.team_id == incident.team_id, Alert.id.in_(ids))).all() if ids else []
    by_fingerprint = {row.fingerprint: row for row in accepted}
    members = [by_fingerprint.get(row.fingerprint, row) for row in current]
    return sorted(members, key=lambda row: (utc(cursors[row.fingerprint].get("first_at", cursors[row.fingerprint]["at"]))
        if row.fingerprint in cursors else utc(row.timestamp), row.fingerprint))


def update_members(session, incident, group, policy, at, canonical):
    resolved = [member.status == "resolved" for member in canonical]
    context = copy.deepcopy(incident.lifecycle_context)
    context["members"] = {"total": len(resolved), "resolved": sum(resolved), "active": len(resolved) - sum(resolved),
        "resolution": "full" if resolved and all(resolved) else "partial" if any(resolved) else "none"}
    state = copy.deepcopy(group.lifecycle_state)
    context["flapping"] = flap(state, policy, at)
    context["transition_times"] = state["transitions"]
    incident.lifecycle_context, group.lifecycle_state = context, state
    session.add(group)
    session.add(incident)
    # Old resolved members remain linked/history, but cannot immediately close
    # a reopened episode under first/last policies. Continuing active members
    # and versions admitted in this episode retain their canonical order.
    episode = [member.status == "resolved" for member in canonical
        if state["members"].get(member.fingerprint, {}).get("episode", context["episode"]) == context["episode"]]
    should_resolve = bool(resolved) and (policy["resolve_on"] == "all_resolved" and all(resolved)
        or policy["resolve_on"] == "first_resolved" and bool(episode) and episode[0]
        or policy["resolve_on"] == "last_resolved" and bool(episode) and episode[-1])
    if should_resolve and incident.status in IncidentStatus.get_active(True):
        transition(session, incident, "resolved", at=at, reason="canonical member resolution", group=group)


def project(incident, *, now=None):
    context = copy.deepcopy(incident.lifecycle_context)
    if not context or context.get("team_id") != incident.team_id:
        return None
    config = context.get("flapping")
    transitions = context.pop("transition_times", [])
    if config and config.get("last_transition_at"):
        now = now or datetime.utcnow()
        config["transition_count"] = sum(utc(value) > now - timedelta(seconds=config["window_seconds"]) for value in transitions)
        if now >= utc(config["last_transition_at"]) + timedelta(seconds=config["reset_after_seconds"]):
            config.update(active=False, since=None, transition_count=0)
    return context
