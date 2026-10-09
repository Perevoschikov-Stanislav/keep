"""Validated candidates and atomic publication of existing Keep configuration."""

import ast
import copy
import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import celpy
from pydantic import parse_obj_as
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from keep.api.core import db
from keep.api.core.incident_contract import (
    ContractError, PROTECTED, SCHEMA, parse_yaml, read_yaml, require, validate_bundle,
    validate_workflow_shape, reject_sensitive, sensitive_key,
)
from keep.api.models.db.extraction import ExtractionRule, ExtractionRuleDtoBase
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_configuration import (
    IncidentConfiguration, IncidentConfigurationVersion, ManagedIncidentResource,
)
from keep.api.models.db.mapping import MappingRule, MappingRuleDtoIn
from keep.api.models.db.rule import Rule, ResolveOn, CreateIncidentOn
from keep.api.models.db.workflow import Workflow, WorkflowVersion


MODELS = {"mappings": MappingRule, "extraction": ExtractionRule, "rules": Rule, "workflows": Workflow}
DERIVED_POLICIES = ("normalization", "presentations", "correlation", "lifecycle", "automation", "contacts", "routes", "notification_policies")
FUTURE = ()
INTEGRATIONS = ("transports", "destinations", "proof_profiles", "service_clients", "subscribers")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def defaults(value, schema):
    schema = SCHEMA["$defs"].get(schema.get("$ref", "").split("/")[-1], schema)
    if isinstance(value, dict):
        result = copy.deepcopy(value)
        for name, definition in schema.get("properties", {}).items():
            if name not in result and "default" in definition:
                result[name] = copy.deepcopy(definition["default"])
            if name in result:
                result[name] = defaults(result[name], definition)
        return result
    if isinstance(value, list):
        return [defaults(item, schema.get("items", {})) for item in value]
    return value


def compile_cel(value, location):
    if value:
        try:
            celpy.Environment().compile(value)
        except Exception:
            raise ContractError(location + ": invalid CEL") from None


def workflow_values(document, location="workflow"):
    """Validate the current syntax without executing providers or resolving secrets."""
    from keep.providers.providers_factory import ProvidersFactory
    from keep.workflowmanager.workflowstore import WorkflowStore

    validate_workflow_shape(document, location)
    workflow = document["workflow"]
    for section in ("inputs", "consts"):
        reject_sensitive(workflow.get(section, {}), location + "." + section)
    metadata = WorkflowStore.pre_parse_workflow_yaml(copy.deepcopy(document))
    for trigger in workflow["triggers"]:
        require(isinstance(trigger, dict) and trigger.get("type") in {"manual", "alert", "incident", "interval"},
                location + ".triggers", "unsupported trigger type")
        require(not set(trigger) - {"type", "filters", "value", "cel", "events"}, location + ".triggers", "unknown trigger field")
        if trigger["type"] == "interval":
            require(metadata.interval > 0, location + ".triggers", "interval must be positive")
        compile_cel(trigger.get("cel"), location + ".triggers.cel")
    names = set()
    for section, method_name in (("steps", "_query"), ("actions", "_notify")):
        for step in workflow.get(section, []):
            require(step["name"] not in names, location, "duplicate step/action name")
            names.add(step["name"])
            require(not set(step) - {"name", "provider", "notification", "condition", "if", "foreach", "vars",
                                    "on-failure", "continue", "continue_on_error", "throttle", "enrich_alert", "enrich_incident"},
                    location, "unknown step/action field")
            provider = step["provider"]
            require(not set(provider) - {"type", "with", "config", "on-failure"}, location, "unknown provider field")
            try:
                provider_class = ProvidersFactory.get_provider_class(provider["type"])
                signature = inspect.signature(getattr(provider_class, method_name))
                parameters = provider["with"]
                signature.bind(None, **parameters)
                for key, value in parameters.items():
                    parameter = signature.parameters.get(key)
                    if parameter and parameter.annotation is not inspect.Parameter.empty and "{{" not in str(value):
                        parse_obj_as(parameter.annotation, value)
            except Exception:
                raise ContractError(location + ": invalid provider or method parameters") from None
            expression = step.get("if")
            if expression is not None:
                require(isinstance(expression, str), location + ".if", "existing expression string required")
                try:
                    ast.parse(re.sub(r"{{.*?}}", "None", expression), mode="eval")
                except (SyntaxError, ValueError):
                    raise ContractError(location + ".if: invalid workflow expression") from None
            foreach = step.get("foreach")
            if foreach is not None:
                references = [foreach] if isinstance(foreach, str) else foreach
                require(isinstance(references, list) and references and all(
                    isinstance(reference, str) and re.findall(r"{{\s*[^}]+\s*}}", reference) for reference in references),
                    location + ".foreach", "existing context references required")
    return {"name": metadata.name, "description": metadata.description, "interval": metadata.interval,
            "is_disabled": metadata.disabled, "workflow_raw": json.dumps(workflow, sort_keys=True, ensure_ascii=False)}


def resource_values(kind, document):
    try:
        if kind == "mappings":
            require(not set(document) - set(MappingRuleDtoIn.__fields__), kind, "unknown mapping field")
            values = MappingRuleDtoIn(**document).dict()
            for row in values.get("rows") or []:
                require(not {key.split(".")[0] for key in row} & PROTECTED, kind, "mapping cannot write canonical security fields")
                reject_sensitive(row, kind + ".rows")
            output = values.get("new_property_name")
            require(not output or output.split(".")[0] not in PROTECTED, kind, "unsafe mapping output")
            return {**values, "disabled": False, "override": True, "condition": None}
        if kind == "extraction":
            require(not set(document) - set(ExtractionRuleDtoBase.__fields__), kind, "unknown extraction field")
            values = ExtractionRuleDtoBase(**document).dict()
            expression = re.compile(values["regex"])
            require(not set(expression.groupindex) & PROTECTED, kind, "extraction cannot write canonical security fields")
            require(not any(sensitive_key(key) for key in expression.groupindex), kind, "credential extraction forbidden")
            require(values["attribute"] and values["attribute"].split(".")[0] not in PROTECTED, kind, "invalid extraction attribute")
            compile_cel(values.get("condition"), kind + ".condition")
            return values
        if kind == "rules":
            from keep.api.routes.rules import RuleCreateDto
            require(not set(document) - set(RuleCreateDto.__fields__), kind, "unknown existing correlation field")
            dto = RuleCreateDto(**document)
            require(dto.ruleName and dto.celQuery and dto.timeframeInSeconds > 0 and dto.threshold > 0,
                    kind, "invalid rule name/window/threshold")
            require(dto.sqlQuery.get("sql") and isinstance(dto.sqlQuery.get("params"), dict), kind, "SQL and params required")
            require(dto.resolveOn in {item.value for item in ResolveOn} and dto.createOn in {item.value for item in CreateIncidentOn},
                    kind, "unknown lifecycle value")
            require(all(isinstance(field, str) and field.split(".")[0] not in PROTECTED for field in dto.groupingCriteria),
                    kind, "invalid grouping field")
            compile_cel(dto.celQuery, kind + ".celQuery")
            return {"name": dto.ruleName, "definition": dto.sqlQuery, "definition_cel": dto.celQuery,
                    "timeframe": dto.timeframeInSeconds, "timeunit": dto.timeUnit, "grouping_criteria": dto.groupingCriteria,
                    "group_description": dto.groupDescription, "require_approve": dto.requireApprove,
                    "resolve_on": dto.resolveOn, "create_on": dto.createOn,
                    "incident_name_template": dto.incidentNameTemplate, "incident_prefix": dto.incidentPrefix,
                    "multi_level": dto.multiLevel, "multi_level_property_name": dto.multiLevelPropertyName,
                    "threshold": dto.threshold, "assignee": dto.assignee, "item_description": None}
        return workflow_values(document)
    except ContractError:
        raise
    except Exception:
        raise ContractError(kind + ": existing Keep format rejected artifact") from None


@dataclass(frozen=True)
class Candidate:
    bundle: dict
    documents: dict
    resources: list
    digest: str
    source: str
    artifact_contents: dict

    @classmethod
    def load(cls, bundle, directory, tenant_id, source="api", artifact_contents=None):
        bundle = copy.deepcopy(bundle)
        checked = validate_bundle(bundle, Path(directory), tenant_id, compatibility=True,
                                  include_documents=True, artifact_contents=artifact_contents)
        for name in FUTURE:
            require(not bundle.get(name), name, "runtime not implemented; retain active configuration")
        if any(bundle.get(name) for name in INTEGRATIONS):
            from keep.identitymanager.team_policy import is_team_scoping_active
            require(is_team_scoping_active(), "integrations", "delegated integrations require OAUTH2PROXY")
        from keep.api.core.notification_adapters import validate_adapters
        validate_adapters(bundle)
        effective = defaults(bundle, SCHEMA["$defs"]["Bundle"])
        from keep.api.core.event_normalization import validate_presentations
        validate_presentations(effective)
        from keep.api.core.incident_correlation import validate_correlation
        validate_correlation(effective)
        for route in effective["routes"]:
            compile_cel(route["match"], "routes." + route["id"] + ".match")
        documents = checked["documents"]
        automated = {ref for policy in effective["automation"] for level in policy["levels"] for ref in level["workflow_refs"]}
        automated.update(policy["ticket"]["workflow_ref"] for policy in effective["automation"] if policy.get("ticket"))
        for ref in automated:
            workflow = documents["workflows." + ref]["workflow"]
            actions = workflow.get("actions", []) + ([workflow["on-failure"]] if workflow.get("on-failure") else [])
            require(all(type(action.get("notification")) is bool for action in actions),
                    "automation.workflow_refs.notification", "automated actions must explicitly declare notification true/false")
        documents["access"] = defaults(documents["access"], SCHEMA["$defs"]["TeamPolicy"])
        for view in documents["access"].get("incident_views", []):
            compile_cel(view["cel"], "access.incident_views.cel")
        resources = []
        for team in documents["access"].get("teams", []):
            resources.append({"kind": "teams", "id": team["id"], "data": team, "digest": digest(team)})
        for kind in (*MODELS, *INTEGRATIONS, *DERIVED_POLICIES):
            for item in effective.get(kind, []):
                data = resource_values(kind, documents[kind + "." + item["id"]]) if kind in MODELS else item
                resources.append({"kind": kind, "id": item["id"], "data": data,
                                  "digest": digest({"data": data, "artifact": item.get("artifact")})})
        require(len({(item["kind"], item["id"]) for item in effective["adoptions"]}) == len(effective["adoptions"]),
                "adoptions", "duplicate adoption")
        effective_digest = digest({"bundle": effective, "documents": documents})
        return cls(effective, documents, resources, effective_digest, source, checked["artifact_contents"])

    @classmethod
    def from_file(cls, path, tenant_id):
        path = Path(path)
        return cls.load(read_yaml(path), path.parent, tenant_id, source=str(path.absolute()))


def target_row(session, tenant_id, kind, target_id, *, lock=False):
    model = MODELS[kind]
    try:
        identifier = int(target_id) if kind in {"mappings", "extraction"} else UUID(target_id) if kind == "rules" else target_id
    except (ValueError, TypeError):
        raise ContractError(kind + ": invalid target ID") from None
    query = select(model).where(model.tenant_id == tenant_id, model.id == identifier)
    if lock:
        query = query.with_for_update()
    return session.exec(query).first()


def current_values(kind, row):
    fields = set(MappingRuleDtoIn.__fields__) | {"disabled", "override", "condition"} if kind == "mappings" else (
        set(ExtractionRuleDtoBase.__fields__) if kind == "extraction" else
        {"name", "description", "interval", "is_disabled", "workflow_raw"} if kind == "workflows" else
        set(Rule.__fields__) - {"id", "tenant_id", "created_by", "creation_time", "updated_by", "update_time", "is_deleted"})
    return {key: getattr(row, key) for key in sorted(fields)}


def resource_drift(resource, target, bundle):
    kind = resource["kind"]
    if target is None or current_values(kind, target) != resource["data"] or getattr(target, "is_deleted", False):
        return True
    if kind in {"mappings", "workflows"}:
        flag = "is_provisioned" if kind == "mappings" else "provisioned"
        filename = next(item["artifact"]["path"] for item in bundle[kind] if item["id"] == resource["id"])
        return not getattr(target, flag) or target.provisioned_file != filename
    return False


class IncidentProvisioning:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    def _plan(self, session, candidate):
        require(candidate.bundle["tenant_id"] == self.tenant_id, "tenant", "candidate belongs to another tenant")
        active = session.get(IncidentConfiguration, self.tenant_id)
        owned = session.exec(select(ManagedIncidentResource).where(ManagedIncidentResource.tenant_id == self.tenant_id)).all()
        previous = {(row.kind, row.logical_id): row for row in owned}
        baseline = {(item["kind"], item["id"]): item for item in active.snapshot["resources"]} if active and active.digest else {}
        desired = {(item["kind"], item["id"]): item for item in candidate.resources}
        removals = {(item["kind"], item["id"]): item for item in candidate.bundle["deletions"]}
        adoptions = {(item["kind"], item["id"]): item for item in candidate.bundle["adoptions"]}
        require(not set(adoptions) - set(desired), "adoptions", "adoption must reference a desired resource")
        changes = []
        for key, row in previous.items():
            require(row.bundle_id == candidate.bundle["id"], "ownership", "resource belongs to another bundle")
            if row.deleted:
                continue
            require(key in desired or key in removals, "deletions", "managed resource omitted without explicit deletion")
        for key, removal in removals.items():
            row = previous.get(key)
            require(row is not None and row.bundle_id == candidate.bundle["id"] and row.digest == removal["expected_resource_digest"],
                    "deletions", "unknown ownership or stale resource digest")
            if row.deleted:
                continue
            self._check_delete(session, row)
            changes.append({"operation": "delete", "kind": key[0], "id": key[1], "target_id": row.target_id,
                            "resource_digest": row.digest})
        for key, item in desired.items():
            row = previous.get(key)
            adoption = adoptions.get(key)
            target = target_row(session, self.tenant_id, key[0], row.target_id, lock=True) if row and key[0] in MODELS and row.target_id else None
            if adoption:
                if row:
                    require(row.target_id == adoption["target_id"], "adoptions", "managed binding cannot be changed by adoption")
                else:
                    require(not any(entry.kind == key[0] and entry.target_id == adoption["target_id"] for entry in owned),
                            "adoptions", "target already has an owner")
                    target = target_row(session, self.tenant_id, key[0], adoption["target_id"], lock=True)
                    require(target is not None and not getattr(target, "is_deleted", False), "adoptions", "target not found in this tenant")
                    require(digest(current_values(key[0], target)) == adoption["expected_resource_digest"], "adoptions", "target changed since adoption review")
            elif key[0] in MODELS and target is None:
                model = MODELS[key[0]]
                collision = session.exec(select(model).where(model.tenant_id == self.tenant_id, model.name == item["data"]["name"])).first()
                require(collision is None or getattr(collision, "is_deleted", False), "ownership", "name collision; explicit adoption required")
            drift = key in baseline and key[0] in MODELS and resource_drift(baseline[key], target, active.snapshot["bundle"])
            changed = not row or row.deleted or row.digest != item["digest"] or drift or (key[0] in MODELS and target is None)
            if changed:
                changes.append({"operation": "adopt" if adoption and (not row or row.deleted) else "create" if not row or row.deleted else "update",
                                "kind": key[0], "id": key[1], "target_id": str(target.id) if target else row.target_id if row else None,
                                "resource_digest": item["digest"], "drift": drift})
        plan = {"active_digest": active.digest if active else None, "candidate_digest": candidate.digest,
                "generation": active.generation if active else 0, "changes": changes,
                "revision": candidate.bundle["revision"], "source": candidate.source,
                "result": "noop" if active and active.digest == candidate.digest and not changes else "apply"}
        if any(item["kind"] == "normalization" for item in changes):
            plan["normalization_impact"] = {"scope": "new_events", "rewrite_history": False,
                                           "identity_transition": "review migration plan before changing fingerprint/grouping fields"}
        changed_rules = {item["id"] for item in changes if item["kind"] == "correlation"}
        if any(item["kind"] == "lifecycle" for item in changes) or (active and active.digest and
                active.snapshot["bundle"].get("correlation_overlap") != candidate.bundle["correlation_overlap"]):
            changed_rules.update(item["id"] for item in candidate.bundle["correlation"])
        if changed_rules:
            affected_rules = set(changed_rules)
            scope_teams = {team for item in candidate.bundle["correlation"] if item["id"] in changed_rules for team in item["team_ids"]}
            if candidate.bundle["correlation_overlap"] == "first_match":
                affected_rules.update(item["id"] for item in candidate.bundle["correlation"] if scope_teams.intersection(item["team_ids"]))
            open_contexts = session.exec(select(Incident.correlation_context).where(Incident.tenant_id == self.tenant_id,
                Incident.status.in_(('firing', 'acknowledged')), Incident.correlation_context.isnot(None))).all()
            plan["correlation_impact"] = {"scope": "new_groups", "rewrite_history": False,
                "existing_incidents": "keep pinned rule and membership; next accepted event uses new rule version",
                "changed_rules": sorted(changed_rules),
                "affected_open_incidents": sum(1 for context in open_contexts if context.get("rule_id") in affected_rules)}
        automation_changes = {item["id"] for item in changes if item["kind"] == "automation"}
        if automation_changes:
            states = session.exec(select(Incident.automation_context).where(Incident.tenant_id == self.tenant_id,
                Incident.status.in_(("firing", "acknowledged")), Incident.automation_context.isnot(None))).all()
            plan["automation_impact"] = {"policies": {item["id"]: item["on_policy_update"] for item in candidate.bundle["automation"]
                if item["id"] in automation_changes}, "affected_open_incidents": sum(
                    1 for state in states if state.get("policy_id") in automation_changes),
                "deleted_policy": "current chain pins its policy; next episode has no automation",
                "reschedule_origin": "existing SLA origin", "repeat_apply": "same policy digest keeps one chain"}
        if candidate.bundle.get("runtime_ownership") or (active and active.snapshot.get("bundle", {}).get("runtime_ownership")):
            from keep.api.core.incident_runtime_ownership import projected_ownership
            plan["runtime_ownership"] = {"before": projected_ownership(active.snapshot) if active and active.digest else [],
                "after": projected_ownership({"bundle": candidate.bundle, "documents": candidate.documents}),
                "external_bridge": "declaration only; stop legacy sender/timers/buttons separately before cutover"}
        from keep.api.core.notification_policies import preview_notifications
        samples = preview_notifications(candidate.bundle)
        if samples:
            plan["notification_preview"] = samples
        plan["preview_digest"] = digest(plan)
        return plan

    def _check_delete(self, session, row):
        if row.kind == "teams":
            from keep.api.models.db.alert import Alert
            from keep.api.models.db.silence import Silence
            for model in (Alert, Incident, Silence):
                require(session.exec(select(model).where(model.tenant_id == self.tenant_id, model.team_id == row.logical_id).limit(1)).first() is None,
                        "deletions.teams", "team has operator/history data; reviewed ownership migration required")
        if row.kind == "rules":
            require(session.exec(select(Incident).where(Incident.tenant_id == self.tenant_id, Incident.rule_id == UUID(row.target_id)).limit(1)).first() is None,
                    "deletions.rules", "rule is referenced by incidents")

    def preview(self, candidate):
        with Session(db.engine) as session:
            return self._plan(session, candidate)

    def apply(self, candidate, *, expected_active_digest, expected_candidate_digest, expected_preview_digest, actor):
        require(candidate.digest == expected_candidate_digest, "apply", "candidate changed since preview")
        try:
            with Session(db.engine, expire_on_commit=False) as session:
                with session.begin():
                    active = session.exec(select(IncidentConfiguration).where(IncidentConfiguration.tenant_id == self.tenant_id).with_for_update()).first()
                    require((active.digest if active else None) == expected_active_digest, "apply", "active version changed; preview again")
                    plan = self._plan(session, candidate)
                    require(plan["preview_digest"] == expected_preview_digest, "apply", "resource state changed; preview again")
                    if plan["result"] == "noop":
                        return plan
                    if active is None:
                        active = IncidentConfiguration(tenant_id=self.tenant_id)
                        session.add(active)
                        session.flush()
                    generation = active.generation + 1
                    result = session.exec(update(IncidentConfiguration).where(
                        IncidentConfiguration.tenant_id == self.tenant_id,
                        IncidentConfiguration.generation == active.generation,
                    ).values(generation=generation))
                    require(result.rowcount == 1, "apply", "concurrent apply conflict")
                    now = datetime.now(timezone.utc)
                    for change in plan["changes"]:
                        if change["operation"] != "delete":
                            continue
                        owner = session.get(ManagedIncidentResource, (self.tenant_id, change["kind"], change["id"]))
                        if change["kind"] in MODELS:
                            target = target_row(session, self.tenant_id, change["kind"], owner.target_id)
                            if target:
                                if change["kind"] in {"rules", "workflows"}:
                                    target.is_deleted = True
                                else:
                                    target.disabled = True
                                session.add(target)
                        owner.deleted = True
                        owner.generation = generation
                        session.add(owner)
                    entries = []
                    changes = {(item["kind"], item["id"]): item for item in plan["changes"]}
                    for resource in candidate.resources:
                        key = (resource["kind"], resource["id"])
                        owner = session.get(ManagedIncidentResource, (self.tenant_id, *key))
                        change = changes.get(key)
                        target = None
                        if key[0] in MODELS:
                            target_id = change["target_id"] if change else owner.target_id
                            target = target_row(session, self.tenant_id, key[0], target_id) if target_id else None
                            if change:
                                target = self._write_resource(session, key[0], target, resource["data"], actor, now,
                                                              candidate.bundle.get(key[0], []), key[1])
                            # SQLAlchemy expires server-generated timestamps on update.
                            # Materialize them before building the persisted runtime snapshot.
                            session.refresh(target)
                        target_id = str(target.id) if target else None
                        if owner is None:
                            owner = ManagedIncidentResource(tenant_id=self.tenant_id, kind=key[0], logical_id=key[1],
                                                            bundle_id=candidate.bundle["id"], digest=resource["digest"],
                                                            revision=candidate.bundle["revision"], generation=generation)
                        owner.target_id, owner.digest, owner.revision, owner.generation, owner.deleted = (
                            target_id, resource["digest"], candidate.bundle["revision"], generation, False)
                        session.add(owner)
                        entries.append({**resource, "target_id": target_id,
                                        "runtime": json.loads(target.json()) if target else None})
                    snapshot = {"digest": candidate.digest, "bundle": candidate.bundle, "documents": candidate.documents,
                                "artifact_contents": candidate.artifact_contents,
                                "resources": entries, "generation": generation, "source": candidate.source,
                                "applied_by": actor, "applied_at": now.isoformat()}
                    old_presentations = (active.snapshot or {}).get("bundle", {}).get("presentations") if active and active.snapshot else None
                    active.generation, active.digest, active.snapshot = generation, candidate.digest, snapshot
                    active.source, active.updated_by, active.updated_at = candidate.source, actor, now
                    session.add(active)
                    if candidate.bundle.get("presentations"):
                        from keep.api.models.db.incident import Incident, IncidentStatus
                        from keep.api.core.event_normalization import refresh_incident_presentation
                        active_incidents = session.exec(select(Incident).where(
                            Incident.tenant_id == self.tenant_id,
                            Incident.status.in_(IncidentStatus.get_active(True)),
                        )).all()
                        for inc in active_incidents:
                            if inc.normalization_context:
                                refresh_incident_presentation(self.tenant_id, inc, session, snapshot=snapshot)
                    session.add(IncidentConfigurationVersion(tenant_id=self.tenant_id, generation=generation,
                                digest=candidate.digest, snapshot=snapshot, source=candidate.source, applied_by=actor, applied_at=now))
                from keep.api.core.incident_configuration import published_configuration
                published_configuration(db.engine, self.tenant_id, snapshot)
                return {**plan, "generation": generation, "result": "applied"}
        except (IntegrityError, OperationalError):
            raise ContractError("apply: write conflict/failure; active configuration retained") from None

    def _write_resource(self, session, kind, target, values, actor, now, artifacts, logical_id):
        if target is None:
            extra = {"created_by": actor}
            if kind == "rules":
                extra["creation_time"] = now
            if kind == "workflows":
                extra["id"] = str(uuid4())
            target = MODELS[kind](tenant_id=self.tenant_id, **extra, **values)
        else:
            for key, value in values.items():
                setattr(target, key, copy.deepcopy(value))
            target.updated_by = actor
            if kind == "workflows":
                target.revision += 1
        if kind in {"workflows", "rules"}:
            target.is_deleted = False
        if kind in {"workflows", "mappings"}:
            filename = next(item["artifact"]["path"] for item in artifacts if item["id"] == logical_id)
            if kind == "workflows":
                target.provisioned = True
            else:
                target.is_provisioned = True
            target.provisioned_file = filename
        session.add(target)
        session.flush()
        if kind == "workflows":
            for version in session.exec(select(WorkflowVersion).where(WorkflowVersion.workflow_id == target.id)).all():
                version.is_current = False
                session.add(version)
            session.add(WorkflowVersion(workflow_id=target.id, revision=target.revision, workflow_raw=target.workflow_raw,
                                        updated_by=actor, updated_at=now, is_valid=True, is_current=True))
            session.flush()
        return target

    def status(self):
        from keep.api.core.incident_runtime_ownership import projected_ownership
        with Session(db.engine) as session:
            row = session.get(IncidentConfiguration, self.tenant_id)
            if not row or not row.digest:
                return {"active_digest": None, "generation": 0, "resources": [], "drift": []}
            drift = []
            resources = []
            for item in row.snapshot["resources"]:
                resources.append({"kind": item["kind"], "id": item["id"], "target_id": item["target_id"], "digest": item["digest"]})
                if item["kind"] in MODELS:
                    target = target_row(session, self.tenant_id, item["kind"], item["target_id"])
                    if resource_drift(item, target, row.snapshot["bundle"]):
                        drift.append({"kind": item["kind"], "id": item["id"]})
            return {"active_digest": row.digest, "generation": row.generation,
                    "result": "applied",
                    "revision": row.snapshot["bundle"]["revision"], "source": row.source,
                    "applied_by": row.updated_by, "applied_at": row.updated_at.isoformat(),
                    "resources": resources, "drift": drift,
                    "runtime_ownership": projected_ownership(row.snapshot)}

    def restore_candidate(self, generation, *, deletions=None):
        with Session(db.engine) as session:
            version = session.exec(select(IncidentConfigurationVersion).where(
                IncidentConfigurationVersion.tenant_id == self.tenant_id,
                IncidentConfigurationVersion.generation == generation,
            )).first()
            require(version is not None, "restore", "configuration version not found in this tenant")
            bundle = copy.deepcopy(version.snapshot["bundle"])
            if deletions is not None:
                bundle["deletions"] = deletions
            return Candidate.load(bundle, Path("."), self.tenant_id,
                                  source="version:" + str(generation),
                                  artifact_contents=version.snapshot["artifact_contents"])
