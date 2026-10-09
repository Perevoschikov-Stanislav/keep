"""Durable incident timers and workflow operations. Transport dispatch belongs to 29."""

import copy
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlmodel import Session, select, or_, and_

from keep.api.core.incident_contract import ContractError, validate_link_url
from keep.api.core.incident_lifecycle import lock_incident, utc
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_automation import IncidentAutomationOperation as Operation
from keep.api.models.db.incident_configuration import IncidentConfiguration
from keep.api.models.db.workflow import Workflow, WorkflowExecution, WorkflowToIncidentExecution


class AutomationCancelled(ContractError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def snapshot_in(session, tenant_id):
    row = session.get(IncidentConfiguration, tenant_id)
    return row.snapshot if row and row.digest else None


def incident_metadata(session, incident):
    from keep.api.models.db.alert import AlertEnrichment
    # Preserve pre-existing SQLite/legacy UUID storage as well as API string IDs.
    rows = session.exec(select(AlertEnrichment).where(AlertEnrichment.tenant_id == incident.tenant_id,
        AlertEnrichment.alert_fingerprint.in_((str(incident.id), incident.id.hex)))).all()
    ticket_key = (incident.automation_context or {}).get("policy", {}).get("ticket", {}).get("enrichment_key", "ticket_url")
    return next((row for row in rows if row.enrichments.get(ticket_key)),
                next((row for row in rows if row.alert_fingerprint == str(incident.id)), rows[0] if rows else None))


def selected_policy(incident, snapshot):
    correlation = incident.correlation_context or {}
    if correlation.get("team_id") != incident.team_id or not snapshot:
        return None
    ref = correlation.get("policy", {}).get("automation_ref")
    return next((item for item in snapshot["bundle"].get("automation", [])
                 if item["id"] == ref and incident.team_id in item["team_ids"]), None)


def stopped(incident, state):
    if state.get("team_id") != incident.team_id:
        return "ownership_changed"
    if state.get("cancelled"):
        return "policy_cancelled"
    if incident.status in {"merged", "deleted"} or incident.status in state["policy"]["stop_on"]:
        return incident.status
    return None


def cancel_pending(session, incident, reason, now, *, chain=None):
    query = select(Operation).where(Operation.tenant_id == incident.tenant_id,
        Operation.incident_id == incident.id, Operation.status.in_(("pending", "awaiting_dispatch")))
    if chain:
        query = query.where(Operation.chain_id == chain)
    for operation in session.exec(query).all():
        operation.status, operation.completed_at = "cancelled", now
        operation.result = {"reason": reason}
        session.add(operation)


def _new_state(incident, policy, snapshot, episode, origin, first_origin):
    version = digest(policy)
    refs = {ref for level in policy["levels"] for ref in level.get("workflow_refs", [])}
    if policy.get("ticket"):
        refs.add(policy["ticket"]["workflow_ref"])
    bindings = {entry["id"]: {"id": entry["target_id"], "revision": entry["runtime"]["revision"]}
                for entry in snapshot["resources"] if entry["kind"] == "workflows" and entry["id"] in refs}
    return {"team_id": incident.team_id, "episode": episode, "policy_id": policy["id"], "policy_version": version,
        "config_digest": snapshot["digest"], "policy": copy.deepcopy(policy), "bindings": bindings,
        "contacts": [copy.deepcopy(item) for item in snapshot["bundle"].get("contacts", []) if item["team_id"] == incident.team_id],
        "destinations": [copy.deepcopy(item) for item in snapshot["bundle"].get("destinations", []) if item["team_id"] == incident.team_id],
        "keep_url": snapshot["bundle"]["keep_url"], "origin": origin.isoformat(), "first_origin": first_origin.isoformat(),
        "chain_id": digest([str(incident.id), episode, version]), "cursors": {}, "level": None,
        "ack_deadline_at": (origin + timedelta(seconds=policy["ack_deadline_seconds"])).isoformat(), "ack_breached": False}


def sync_incident(session, incident, *, now, snapshot=None):
    """Caller holds the common tenant/incident locks; state commits with lifecycle."""
    now = utc(now)
    from keep.api.core.incident_runtime_ownership import gate_reason
    if gate_reason(snapshot, incident.team_id, "domain"):
        return
    state = copy.deepcopy(incident.automation_context)
    lifecycle = incident.lifecycle_context or {}
    episode = lifecycle.get("episode", 1)
    policy = selected_policy(incident, snapshot)
    if state and state.get("team_id") != incident.team_id:
        cancel_pending(session, incident, "ownership_changed", now)
        return
    if policy:
        from keep.api.core.incident_correlation import matches
        if not matches(policy.get("match", "true"), {"normalized": (incident.normalization_context or {}).get("normalized", {}),
            "incident": {"status": incident.status, "severity": incident.severity}, "team_id": incident.team_id}):
            if state:
                cancel_pending(session, incident, "policy_condition_false", now)
                state.update(cancelled=True, match_blocked=True, stopped_reason="policy_condition_false", next_due_at=None,
                    last_decision={"reason": "policy_condition_false", "at": now.isoformat()})
                incident.automation_context = state
                session.add(incident)
            return
        if state and state.get("match_blocked"):
            # Resume the same timer/cursors; eligibility does not reset the SLA.
            state.update(cancelled=False, match_blocked=False, stopped_reason=None)
    if state and policy and digest(policy) != state["policy_version"] and episode == state["episode"]:
        update = policy.get("on_policy_update", "next_episode")
        if update == "cancel":
            state.update(cancelled=True, last_decision={"reason": "policy_cancelled", "at": now.isoformat()})
        elif update == "reschedule":
            cancel_pending(session, incident, "policy_rescheduled", now)
            previous = state
            state = _new_state(incident, policy, snapshot, episode, utc(previous["origin"]), utc(previous["first_origin"]))
            state["ticket_done"] = previous.get("ticket_done", False)
            state["last_decision"] = {"reason": "policy_rescheduled", "at": now.isoformat()}
    if state and episode != state["episode"]:
        cancel_pending(session, incident, "episode_changed", now)
        previous = state
        if policy is None and snapshot is not None:
            state.update(episode=episode, cancelled=True)
        elif (policy or previous["policy"])["on_reopen"] == "continue" and not previous.get("cancelled"):
            if policy and digest(policy) != previous["policy_version"]:
                state = _new_state(incident, policy, snapshot, episode, utc(previous["origin"]), utc(previous["first_origin"]))
                state.update(cursors=previous["cursors"], level=previous["level"], ticket_done=previous.get("ticket_done", False))
            else:
                state.update(episode=episode, stopped_reason=None)
        else:
            policy = policy or (previous["policy"] if snapshot is None else None)
            if policy is None:
                state.update(episode=episode, cancelled=True)
            else:
                first_origin = utc(previous["first_origin"])
                origin = utc(lifecycle.get("episode_start", now)) if policy["sla_start"] == "last_reopen" else first_origin
                state = _new_state(incident, policy, snapshot or {
                    "digest": previous["config_digest"], "bundle": {"keep_url": previous["keep_url"],
                        "contacts": previous["contacts"], "destinations": previous["destinations"]},
                    "resources": [{"kind": "workflows", "id": ref, "target_id": binding["id"],
                        "runtime": {"revision": binding["revision"]}} for ref, binding in previous["bindings"].items()]},
                    episode, origin, first_origin)
                state["ticket_done"] = previous.get("ticket_done", False)
    if state is None:
        if not policy or not incident.is_visible or incident.status not in {"firing", "acknowledged"}:
            return
        origin = utc(lifecycle.get("episode_start", incident.creation_time))
        state = _new_state(incident, policy, snapshot, episode, origin, origin)
    deadline = utc(state["ack_deadline_at"])
    ack_at = utc(lifecycle["ack_at"]) if lifecycle.get("ack_at") and incident.status != "firing" else None
    if (ack_at is not None and ack_at > deadline) or (ack_at is None and now >= deadline and not state.get("stopped_reason")):
        state["ack_breached"] = True
    reason = stopped(incident, state)
    state["stopped_reason"] = reason
    if reason:
        cancel_pending(session, incident, reason, now)
        state["next_due_at"] = None
        state["last_decision"] = {"reason": reason, "at": now.isoformat()}
    incident.automation_context = state
    session.add(incident)


def project(incident):
    state = incident.automation_context
    if not state or state.get("team_id") != incident.team_id:
        return None
    keys = ("episode", "chain_id", "policy_id", "policy_version", "config_digest", "origin", "ack_deadline_at",
            "ack_breached", "level", "next_due_at", "stopped_reason", "last_decision", "last_result")
    return {key: copy.deepcopy(state.get(key)) for key in keys}


def _silence(session, incident, now):
    from keep.api.bl.silences_evaluator import SilenceEvaluator
    result = SilenceEvaluator(session, incident.tenant_id, now=now).incidents([incident])[incident.id]
    return "silence_partial_payload_unsafe" if result.coverage == "partial" else "silenced" if result.silenced else None


def _operation(session, incident, state, kind, level, ordinal, due, target_kind, ref, *, notification_allowed=True, reason=None):
    # Ticket creation is once per canonical incident, including same-ID reopen.
    identity = [incident.tenant_id, str(incident.id), "ticket", ref] if kind == "ticket" else [
        incident.tenant_id, str(incident.id), state["chain_id"], kind, level, ordinal, target_kind, ref]
    key = digest(identity)
    if session.get(Operation, key):
        return
    context = {"binding": state["bindings"].get(ref), "canonical_revision": (incident.lifecycle_context or {}).get("revision", 0),
        "notification_allowed": notification_allowed, "notification_skip_reason": reason,
        "contact_refs": [], "destination_ref": ref if target_kind == "destination" else None}
    source = state["policy"].get("reminder", {}) if kind == "reminder" else next(
        (item for item in state["policy"]["levels"] if item["id"] == level), {})
    context["contact_refs"] = [item for item in source.get("contact_refs", []) if any(
        contact["id"] == item for contact in state["contacts"])]
    status = "pending" if target_kind == "workflow" else "awaiting_dispatch" if notification_allowed else "skipped"
    operation = Operation(id=key, tenant_id=incident.tenant_id, team_id=incident.team_id, incident_id=incident.id,
        episode=state["episode"], chain_id=state["chain_id"], policy_version=state["policy_version"], kind=kind,
        level_id=level, ordinal=ordinal, target_kind=target_kind, target_ref=ref, due_at=due, status=status, context=context,
        result={"reason": reason} if reason else {})
    session.add(operation)
    if target_kind == "destination":
        state["last_result"] = {"operation_id": key, "status": status, "reason": reason, "at": due.isoformat()}


def materialize(session, incident, now, snapshot):
    from keep.api.core.incident_runtime_ownership import gate_reason
    if gate_reason(snapshot, incident.team_id, "domain"):
        return
    sync_incident(session, incident, now=now, snapshot=snapshot)
    state = copy.deepcopy(incident.automation_context)
    if not state or stopped(incident, state) or not incident.is_visible:
        return
    origin, policy = utc(state["origin"]), state["policy"]
    elapsed = (now - origin).total_seconds()
    cursors = state["cursors"]
    eligible = [item for item in policy["levels"] if item["after_seconds"] <= elapsed]
    current_level = eligible[-1]["id"] if eligible else None
    next_times = [origin + timedelta(seconds=item["after_seconds"]) for item in policy["levels"] if item["after_seconds"] > elapsed]
    if not state["ack_breached"] and incident.status == "firing":
        next_times.append(utc(state["ack_deadline_at"]))
    for level in eligible:
        previous = cursors.get(level["id"], -1)
        cadence, limit = level["repeat_every_seconds"], level["repeat_limit"]
        ordinal = int((elapsed - level["after_seconds"]) // cadence) if cadence else 0
        if limit:
            ordinal = min(ordinal, limit - 1)
        active = level["id"] == current_level
        # Retire repeats on earlier levels; execute their first ordinary workflow once.
        if not active:
            ordinal = 0
        if ordinal > previous:
            silence = _silence(session, incident, now) if active else None
            reason = silence if active else "superseded_level"
            due = origin + timedelta(seconds=level["after_seconds"] + ordinal * cadence)
            for ref in level.get("workflow_refs", []):
                _operation(session, incident, state, "escalation", level["id"], ordinal, due, "workflow", ref,
                           notification_allowed=active and not silence, reason=reason)
            for ref in level.get("destination_refs", []):
                if any(item["id"] == ref for item in state["destinations"]):
                    _operation(session, incident, state, "escalation", level["id"], ordinal, due, "destination", ref,
                               notification_allowed=active and not silence, reason=reason or silence)
            cursors[level["id"]] = ordinal
        if active and cadence and (not limit or ordinal + 1 < limit):
            next_times.append(origin + timedelta(seconds=level["after_seconds"] + (ordinal + 1) * cadence))
    reminder = policy.get("reminder")
    if reminder:
        ordinal = int(elapsed // reminder["every_seconds"])
        if ordinal >= 1 and ordinal > cursors.get("reminder", 0):
            silence = _silence(session, incident, now)
            due = origin + timedelta(seconds=ordinal * reminder["every_seconds"])
            for ref in reminder["destination_refs"]:
                if any(item["id"] == ref for item in state["destinations"]):
                    _operation(session, incident, state, "reminder", "reminder", ordinal, due, "destination", ref,
                               notification_allowed=not silence, reason=silence)
            cursors["reminder"] = ordinal
        next_times.append(origin + timedelta(seconds=(max(0, ordinal) + 1) * reminder["every_seconds"]))
    if policy.get("ticket") and not state.get("ticket_done"):
        _operation(session, incident, state, "ticket", "ticket", 0, origin, "workflow", policy["ticket"]["workflow_ref"])
        state["ticket_done"] = True
    for operation in session.exec(select(Operation).where(Operation.tenant_id == incident.tenant_id,
            Operation.incident_id == incident.id, Operation.chain_id == state["chain_id"],
            Operation.kind == "escalation", Operation.status.in_(("pending", "awaiting_dispatch")))).all():
        reason = "superseded_level" if operation.level_id != current_level else "superseded_repeat" if (
            operation.ordinal < cursors.get(operation.level_id, 0)) else None
        if reason:
            operation.context = {**operation.context, "notification_allowed": False, "notification_skip_reason": reason}
            if operation.target_kind == "destination":
                operation.status, operation.result, operation.completed_at = "skipped", {"reason": reason}, now
            session.add(operation)
    if current_level != state["level"]:
        from keep.api.core.db import add_audit
        from keep.api.models.action_type import ActionType
        add_audit(incident.tenant_id, str(incident.id), "system", ActionType.WORKFLOW_ENRICH,
            f"Automation level {current_level}; episode={state['episode']}; policy={state['policy_version']}", session, commit=False)
    state.update(level=current_level, next_due_at=min(next_times).isoformat() if next_times else None,
                 last_decision={"reason": "timer_evaluated", "at": now.isoformat(), "level": current_level})
    incident.automation_context = state
    session.add(incident)


def invalid_operation(session, incident, operation, snapshot):
    from keep.api.core.incident_runtime_ownership import gate_reason
    reason = gate_reason(snapshot, incident.team_id, "domain")
    if reason:
        return reason
    state = incident.automation_context or {}
    if not state or state.get("team_id") != operation.team_id or incident.team_id != operation.team_id:
        return "ownership_changed"
    if state["chain_id"] != operation.chain_id or state["episode"] != operation.episode:
        return "episode_changed"
    reason = stopped(incident, state)
    if reason:
        return reason
    if state["policy_version"] != operation.policy_version:
        return "policy_changed"
    for ref in operation.context.get("contact_refs", []):
        contact = next((item for item in (snapshot or {}).get("bundle", {}).get("contacts", []) if item["id"] == ref), None)
        if not contact or contact["team_id"] != operation.team_id:
            return "contact_retired_or_reassigned"
    binding = operation.context.get("binding")
    if operation.target_kind == "workflow":
        active = next((item for item in (snapshot or {}).get("resources", []) if item["kind"] == "workflows"
            and item["id"] == operation.target_ref), None)
        workflow = session.get(Workflow, binding["id"]) if binding else None
        if not active or not binding or active["target_id"] != binding["id"] or not workflow or workflow.tenant_id != incident.tenant_id:
            return "workflow_retired"
        if workflow.is_deleted or workflow.is_disabled or workflow.revision != binding["revision"] or active["runtime"]["revision"] != binding["revision"]:
            return "workflow_changed"
    return None


def record_result(session, incident, operation, now):
    state = copy.deepcopy(incident.automation_context)
    if state and state.get("team_id") == operation.team_id and state.get("chain_id") == operation.chain_id:
        state["last_result"] = {"operation_id": operation.id, "status": operation.status, "at": now.isoformat(), **operation.result}
        incident.automation_context = state
        session.add(incident)
    from keep.api.core.db import add_audit
    from keep.api.models.action_type import ActionType
    add_audit(operation.tenant_id, str(incident.id), "system", ActionType.WORKFLOW_ENRICH,
        f"Automation operation {operation.id}; {operation.status}; {operation.result.get('reason', '')}", session, commit=False)


def notification_decision(state, operation, now):
    if not operation.context["notification_allowed"]:
        return operation.context["notification_skip_reason"]
    if operation.kind == "ticket":
        return None
    elapsed = (now - utc(state["origin"])).total_seconds()
    if operation.kind == "reminder":
        return "superseded_repeat" if operation.ordinal < int(elapsed // state["policy"]["reminder"]["every_seconds"]) else None
    eligible = [level for level in state["policy"]["levels"] if level["after_seconds"] <= elapsed]
    if not eligible or eligible[-1]["id"] != operation.level_id:
        return "superseded_level"
    level = eligible[-1]
    if level["repeat_every_seconds"]:
        ordinal = int((elapsed - level["after_seconds"]) // level["repeat_every_seconds"])
        if level["repeat_limit"]:
            ordinal = min(ordinal, level["repeat_limit"] - 1)
        if operation.ordinal < ordinal:
            return "superseded_repeat"
    return None


def check_automation_context(context_manager, *, before_provider=False):
    marker = getattr(context_manager, "automation_operation", None)
    if not marker:
        return
    from keep.api.core import db
    engine = getattr(context_manager, "automation_engine", db.engine)
    now = datetime.utcnow()
    with Session(engine) as session:
        operation = session.get(Operation, marker["id"])
        if operation is None or operation.tenant_id != context_manager.tenant_id:
            raise AutomationCancelled("operation_not_found")
        incident, _ = lock_incident(session, operation.tenant_id, operation.incident_id)
        sync_incident(session, incident, now=now, snapshot=snapshot_in(session, operation.tenant_id))
        operation = session.exec(select(Operation).where(Operation.id == operation.id).with_for_update()
                                 .execution_options(populate_existing=True)).one()
        reason = invalid_operation(session, incident, operation, snapshot_in(session, operation.tenant_id))
        if operation.status != "running" or operation.token != marker["token"]:
            reason = "execution_fenced"
        if (incident.lifecycle_context or {}).get("revision", 0) != marker["revision"]:
            reason = reason or "incident_revision_changed"
        if reason:
            marker["cancelled_reason"] = reason
            session.commit()
            raise AutomationCancelled(reason)
        from keep.api.core.incident_runtime_ownership import gate_reason
        skip = (gate_reason(snapshot_in(session, operation.tenant_id), incident.team_id, "notifications")
                or notification_decision(incident.automation_context, operation, now))
        if skip:
            marker.update(notification_allowed=False, notification_skip_reason=skip)
        if before_provider:
            operation.effect_started = True
            operation.lease_until = now + timedelta(seconds=incident.automation_context["policy"]["execution_lease_seconds"])
            session.add(operation)
        session.commit()


class IncidentAutomationWorker:
    def __init__(self, engine):
        self.engine = engine

    def claim(self, operation_id, now):
        with Session(self.engine, expire_on_commit=False) as session:
            hint = session.get(Operation, operation_id)
            if not hint or hint.target_kind != "workflow":
                return None
            incident, _ = lock_incident(session, hint.tenant_id, hint.incident_id)
            snapshot = snapshot_in(session, hint.tenant_id)
            sync_incident(session, incident, now=now, snapshot=snapshot)
            operation = session.exec(select(Operation).where(Operation.id == operation_id).with_for_update()
                                     .execution_options(populate_existing=True)).one()
            reason = invalid_operation(session, incident, operation, snapshot)
            if operation.status == "running" and operation.lease_until <= now:
                if operation.effect_started:
                    operation.status, operation.completed_at = "uncertain", now
                    operation.result = {"reason": "execution_result_unknown"}
                    session.add(operation)
                    record_result(session, incident, operation, now)
                    execution = session.get(WorkflowExecution, operation.execution_id)
                    if execution:
                        execution.status, execution.is_running, execution.error = "error", 0, "execution_result_unknown"
                        session.add(execution)
                else:
                    operation.status = "pending"
                    operation.token, operation.lease_until = None, None
            if operation.status != "pending" or operation.due_at > now:
                session.commit()
                return None
            if reason:
                operation.status, operation.result, operation.completed_at = "cancelled", {"reason": reason}, now
                session.add(operation)
                record_result(session, incident, operation, now)
                session.commit()
                return None
            if operation.kind == "ticket":
                existing = incident_metadata(session, incident)
                key = incident.automation_context["policy"]["ticket"]["enrichment_key"]
                if existing and existing.enrichments.get(key):
                    operation.status, operation.result, operation.completed_at = "skipped", {"reason": "ticket_link_exists"}, now
                    session.add(operation)
                    record_result(session, incident, operation, now)
                    session.commit()
                    return None
            binding = operation.context["binding"]
            entry = next(item for item in snapshot["resources"] if item["kind"] == "workflows" and item["id"] == operation.target_ref)
            strategy = json.loads(entry["runtime"]["workflow_raw"]).get("strategy", "nonparallel_with_retry")
            if strategy != "parallel":
                busy = session.exec(select(WorkflowExecution.id).join(WorkflowToIncidentExecution,
                    WorkflowToIncidentExecution.workflow_execution_id == WorkflowExecution.id).where(
                    WorkflowExecution.tenant_id == operation.tenant_id, WorkflowExecution.workflow_id == binding["id"],
                    WorkflowExecution.status == "in_progress", WorkflowToIncidentExecution.incident_id == str(incident.id),
                    WorkflowExecution.id != (operation.execution_id or "")).limit(1)).first()
                unknown = session.exec(select(Operation.id).where(Operation.tenant_id == operation.tenant_id,
                    Operation.incident_id == incident.id, Operation.target_ref == operation.target_ref,
                    Operation.target_kind == "workflow", Operation.status == "uncertain", Operation.id != operation.id).limit(1)).first()
                if busy or unknown:
                    reason = "prior_execution_uncertain" if unknown else "workflow_already_running"
                    changed = operation.result.get("reason") != reason
                    operation.result = {"reason": reason}
                    if strategy == "nonparallel":
                        operation.status, operation.completed_at = "skipped", now
                    else:
                        operation.due_at = now + timedelta(seconds=snapshot["bundle"]["dispatch"]["scan_interval_seconds"])
                    session.add(operation)
                    if changed:
                        record_result(session, incident, operation, now)
                    session.commit()
                    return None
            operation.status, operation.token = "running", str(uuid4())
            operation.lease_until = now + timedelta(seconds=incident.automation_context["policy"]["execution_lease_seconds"])
            operation.context = {**operation.context, "canonical_revision": (incident.lifecycle_context or {}).get("revision", 0)}
            execution_id = str(uuid5(NAMESPACE_URL, "keep.incident.automation:" + operation.id))
            if not session.get(WorkflowExecution, execution_id):
                session.add(WorkflowExecution(id=execution_id, tenant_id=operation.tenant_id, workflow_id=binding["id"],
                    workflow_revision=binding["revision"], started=now, triggered_by="incident-automation:" + operation.kind,
                    execution_number=int(operation.id[:8], 16) % 2147483647, status="in_progress", results={}))
                session.flush()
                session.add(WorkflowToIncidentExecution(workflow_execution_id=execution_id, incident_id=str(incident.id)))
            else:
                execution = session.get(WorkflowExecution, execution_id)
                execution.status, execution.is_running, execution.error = "in_progress", 1, None
                session.add(execution)
            operation.execution_id = execution_id
            session.add(operation)
            session.commit()
            return operation

    def execute(self, operation, now):
        from keep.api.core.incident_configuration import configuration_scope
        from keep.api.models.incident import IncidentDto
        from keep.workflowmanager.workflowstore import WorkflowStore
        from keep.workflowmanager.workflowmanager import WorkflowManager
        workflow = None
        status, result = "success", {}
        thread = threading.current_thread()
        missing = object()
        thread_state = {key: getattr(thread, key, missing) for key in (
            "workflow_debug", "workflow_id", "workflow_execution_id", "tenant_id", "step_id")}
        try:
            with configuration_scope(operation.tenant_id):
                binding = operation.context["binding"]
                workflow = WorkflowStore().get_workflow(operation.tenant_id, binding["id"], expected_revision=binding["revision"])
                with Session(self.engine) as session:
                    incident = session.get(Incident, operation.incident_id)
                    event = IncidentDto.from_db_incident(incident, session=session, with_silences=False)
                context = workflow.context_manager
                context.set_incident_context(event)
                context.set_inputs({"automation": {"operation_id": operation.id, "kind": operation.kind,
                    "level": operation.level_id, "episode": operation.episode, "contact_refs": operation.context["contact_refs"]}})
                context.automation_engine = self.engine
                context.automation_operation = {"id": operation.id, "token": operation.token,
                    "revision": operation.context["canonical_revision"], "notification_allowed": operation.context["notification_allowed"],
                    "notification_skip_reason": operation.context["notification_skip_reason"]}
                check_automation_context(context)
                errors, _ = WorkflowManager.get_instance()._run_workflow(workflow, operation.execution_id)
                result["steps"] = {key: {"skipped": bool(value.get("skipped")), "reason": value.get("skip_reason")}
                                   for key, value in context.steps_context.items() if isinstance(value, dict)}
                if errors and any(errors):
                    reason = context.automation_operation.get("cancelled_reason")
                    status, result["reason"] = ("cancelled", reason) if reason else ("failed", "workflow_failed")
                if context.automation_operation.get("cancelled_reason"):
                    status, result["reason"] = "cancelled", context.automation_operation["cancelled_reason"]
                if status == "success" and operation.kind == "ticket":
                    self._ticket_link(operation, workflow)
        except AutomationCancelled as error:
            status, result = "cancelled", {"reason": str(error)}
        except Exception:
            # Do not persist provider exception text: it may contain credentials.
            status, result = "failed", {"reason": "workflow_failed"}
            if workflow and getattr(workflow.context_manager, "automation_operation", {}).get("cancelled_reason"):
                status, result = "cancelled", {"reason": workflow.context_manager.automation_operation["cancelled_reason"]}
        finally:
            for key, value in thread_state.items():
                if value is missing:
                    if hasattr(thread, key):
                        delattr(thread, key)
                else:
                    setattr(thread, key, value)
        now = utc(datetime.utcnow())
        with Session(self.engine) as session:
            incident, _ = lock_incident(session, operation.tenant_id, operation.incident_id)
            current = session.exec(select(Operation).where(Operation.id == operation.id).with_for_update()
                                   .execution_options(populate_existing=True)).one()
            if current.token != operation.token:
                return
            if current.status == "uncertain" and status != "success":
                status, result = "uncertain", {"reason": "execution_result_unknown", "stop_reason": result.get("reason")}
            current.status, current.result, current.completed_at = status, result, now
            execution = session.get(WorkflowExecution, current.execution_id)
            execution.status, execution.is_running = "success" if status == "success" else "error", 0
            execution.error = result.get("reason")
            execution.execution_time = max(0, int((now - utc(execution.started)).total_seconds()))
            session.add(current)
            session.add(execution)
            record_result(session, incident, current, now)
            session.commit()

    def _ticket_link(self, operation, workflow):
        from keep.api.models.db.alert import AlertEnrichment
        from keep.api.core.event_normalization import path_value
        check_automation_context(workflow.context_manager)
        with Session(self.engine) as session:
            incident, _ = lock_incident(session, operation.tenant_id, operation.incident_id)
            state = incident.automation_context
            ticket = state["policy"]["ticket"]
            existing = incident_metadata(session, incident)
            if existing and existing.enrichments.get(ticket["enrichment_key"]):
                return
            data = workflow.context_manager.get_full_context()
            data["incident"] = workflow.context_manager.incident_context.dict()
            data.update(keep_url=state["keep_url"].rstrip("/"), normalized=(incident.normalization_context or {}).get("normalized", {}))
            def replace(match):
                value = path_value(data, match.group(1))
                if value is None or value == "":
                    raise ContractError("Ticket link field missing")
                return str(value) if match.group(1) == "keep_url" else quote(str(value), safe="")
            url = re.sub(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", replace, ticket["url_template"])
            validate_link_url(url, "automation.ticket.url")
            if not existing:
                existing = AlertEnrichment(tenant_id=operation.tenant_id, alert_fingerprint=str(incident.id), enrichments={})
            existing.enrichments = {**existing.enrichments, ticket["enrichment_key"]: url}
            session.add(existing)
            from keep.api.core.db import add_audit
            from keep.api.models.action_type import ActionType
            add_audit(operation.tenant_id, str(incident.id), "system", ActionType.WORKFLOW_ENRICH,
                      "Automation ticket link recorded; operation=" + operation.id, session, commit=False)
            session.commit()

    def due_operations(self, now, limit=100):
        with Session(self.engine) as session:
            return session.exec(select(Operation.id).where(Operation.target_kind == "workflow",
                or_(Operation.status == "pending", and_(Operation.status == "running", Operation.lease_until <= now)),
                Operation.due_at <= now)
                .order_by(Operation.due_at, Operation.id).limit(limit)).all()

    def run_once(self, *, now=None, execute=True):
        now = utc(now or datetime.utcnow())
        with Session(self.engine) as session:
            hints = session.exec(select(Incident.id, Incident.tenant_id).where(Incident.correlation_context.isnot(None),
                Incident.status.in_(("firing", "acknowledged")))).all()
        for incident_id, tenant_id in hints:
            with Session(self.engine) as session:
                incident, _ = lock_incident(session, tenant_id, incident_id)
                materialize(session, incident, now, snapshot_in(session, tenant_id))
                session.commit()
        processed = 0
        if execute:
            for operation_id in self.due_operations(now):
                operation = self.claim(operation_id, now)
                if operation:
                    self.execute(operation, now)
                    processed += 1
        return {"incidents": len(hints), "executed": processed}
