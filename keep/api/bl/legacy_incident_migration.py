"""Read-only bridge snapshots, reviewed adoption and immutable import receipts."""

import copy
import enum
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from keep.api.bl.incident_provisioning import defaults, digest
from keep.api.core.incident_contract import SCHEMA, require, sensitive_key, validate_shape
from keep.api.core.incident_correlation import grouping_keys, matches
from keep.api.core.incident_runtime_ownership import ownership
from keep.api.models.alert import AlertDto
from keep.api.models.db.alert import Alert, AlertAudit, AlertEnrichment, AlertToIncident, LastAlert, LastAlertToIncident
from keep.api.models.db.helpers import NULL_FOR_DELETED_AT
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_configuration import IncidentConfiguration
from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
from keep.api.models.db.incident_migration import LegacyIncidentImport
from keep.api.models.db.tenant import Tenant


def json_value(value):
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, enum.Enum):
        return value.value
    raise TypeError("Not JSON-compatible")


def serial(value):
    return json.loads(json.dumps(value, default=json_value, allow_nan=False))


def redacted(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if sensitive_key(key) else redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def read_bridge_snapshot(path):
    """Only a standalone SQLite backup; opening a live WAL DB is deliberately rejected."""
    path = Path(path).resolve()
    require(path.is_file() and not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")),
            "bridge.snapshot", "use a standalone SQLite backup, not a live WAL database")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute("SELECT incident, key, value FROM state ORDER BY incident, key").fetchall()
        states = {}
        for identifier, key, value in rows:
            require(isinstance(key, str), "bridge.state", "invalid state key")
            require(str(UUID(identifier)) == identifier, "bridge.state", "invalid incident ID")
            if (key.startswith("mm_") or key in {"snooze_until", "snooze_by"}) and not sensitive_key(key):
                states.setdefault(identifier, {})[key] = redacted(json.loads(value))
        return {"digest": digest(rows), "states": states}
    except (sqlite3.Error, ValueError, TypeError):
        from keep.api.core.incident_contract import ContractError
        raise ContractError("bridge.snapshot: invalid SQLite state export") from None


def members(root_id, state):
    value = state.get("mm_members") or []
    value = json.loads(value) if isinstance(value, str) else value
    require(isinstance(value, list) and all(isinstance(item, str) and str(UUID(item)) == item for item in value),
            "bridge.mm_members", "invalid member IDs")
    return sorted(set(value or [root_id]) | {root_id})


def incident_snapshot(session, tenant_id, identifier):
    incident = session.exec(select(Incident).where(Incident.id == UUID(identifier), Incident.tenant_id == tenant_id)).first()
    if not incident:
        return None  # Missing and foreign tenant IDs are indistinguishable.
    links = session.exec(select(LastAlertToIncident).where(
        LastAlertToIncident.tenant_id == tenant_id, LastAlertToIncident.incident_id == incident.id)).all()
    versions = session.exec(select(AlertToIncident).where(
        AlertToIncident.tenant_id == tenant_id, AlertToIncident.incident_id == incident.id)).all()
    fingerprints = sorted({row.fingerprint for row in links})
    alert_ids = [row.alert_id for row in versions]
    alerts = session.exec(select(Alert).where(Alert.tenant_id == tenant_id,
        (Alert.fingerprint.in_(fingerprints)) | (Alert.id.in_(alert_ids))).order_by(Alert.timestamp, Alert.id)).all()
    latest = session.exec(select(LastAlert).where(LastAlert.tenant_id == tenant_id,
        LastAlert.fingerprint.in_(fingerprints)).order_by(LastAlert.fingerprint)).all()
    enrichments = session.exec(select(AlertEnrichment).where(AlertEnrichment.tenant_id == tenant_id,
        AlertEnrichment.alert_fingerprint.in_(fingerprints + [identifier, incident.id.hex]))
        .order_by(AlertEnrichment.alert_fingerprint)).all()
    audit = session.exec(select(AlertAudit).where(AlertAudit.tenant_id == tenant_id,
        AlertAudit.fingerprint.in_((identifier, incident.id.hex))).order_by(AlertAudit.timestamp, AlertAudit.id)).all()
    from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
    from keep.api.core.event_normalization import DERIVED
    projections = {}
    for alert, event in zip(alerts, convert_db_alerts_to_dto_alerts(alerts, with_silences=False, session=session)):
        projections[str(alert.id)] = {key: value for key, value in event.dict().items() if key not in DERIVED}
    data = serial({"incident": incident.dict(), "links": sorted([row.dict() for row in links], key=lambda row: str(row["fingerprint"]) + str(row["deleted_at"])),
        "version_links": sorted([row.dict() for row in versions], key=lambda row: str(row["alert_id"]) + str(row["deleted_at"])),
        "alerts": [row.dict() for row in alerts], "latest": [row.dict() for row in latest],
        "enrichments": [row.dict() for row in enrichments], "audit": [row.dict() for row in audit], "projections": projections})
    return {"digest": digest(data), "data": redacted(data)}


def export_snapshot(session, tenant_id, source_id, bridge):
    require(session.get(Tenant, tenant_id) is not None, "tenant", "unknown trusted tenant")
    roots = {}
    for identifier, state in bridge["states"].items():
        if not state.get("mm_root"):
            try:
                selected = members(identifier, state)
                roots[identifier] = {"members": selected, "state": state, "errors": []}
            except (ValueError, TypeError):
                roots[identifier] = {"members": [identifier], "state": state, "errors": ["invalid_members"]}
    # A child referencing a missing root is retained as an explicit broken mapping.
    for identifier, state in bridge["states"].items():
        root = state.get("mm_root")
        if root and root not in roots:
            roots[identifier] = {"members": [identifier], "state": state, "errors": ["missing_root"]}
        elif root and identifier not in roots[root]["members"]:
            roots[root]["errors"].append("inconsistent_members")
    identifiers = sorted({item for root in roots.values() for item in root["members"]})
    snapshot = {"schema_version": 1, "tenant_id": tenant_id, "source_id": source_id,
        "bridge_digest": bridge["digest"], "roots": roots,
        "incidents": {item: incident_snapshot(session, tenant_id, item) for item in identifiers}}
    snapshot["digest"] = digest(snapshot)
    return snapshot


def candidate_snapshot(candidate):
    return {"digest": candidate.digest, "bundle": candidate.bundle, "documents": candidate.documents,
            "resources": candidate.resources}


def evaluated_groups(exported, snapshot):
    """Recompute normalization, match/overlap and typed keys; ignore legacy mm_key."""
    from keep.api.core.event_normalization import normalize_event
    data, groups, reasons = exported["data"], {}, []
    incident = data["incident"]
    if any(alert["team_id"] != incident["team_id"] for alert in data["alerts"]):
        return {}, ["foreign_alert_history"]
    active = {item["fingerprint"] for item in data["links"] if timestamp(item["deleted_at"]) == NULL_FOR_DELETED_AT}
    rows = {item["id"]: item for item in data["alerts"]}
    latest = [rows[item["alert_id"]] for item in data["latest"] if item["fingerprint"] in active and item["alert_id"] in rows]
    if not latest or len(latest) != len(active):
        reasons.append("missing_history")
    for row in latest:
        payload = copy.deepcopy(data["projections"][row["id"]])
        payload.update(id=row["id"], fingerprint=row["fingerprint"], team_id=incident["team_id"])
        # Reserved fields always come from the canonical DB row, not raw JSON.
        try:
            event = normalize_event(incident["tenant_id"], AlertDto(**payload), snapshot=snapshot)
        except (ValueError, TypeError):
            reasons.append("invalid_alert_payload")
            continue
        selected = False
        for rule in sorted(snapshot["bundle"]["correlation"], key=lambda item: item["priority"], reverse=True):
            if incident["team_id"] not in rule["team_ids"]:
                continue
            try:
                matched = matches(rule["match"], event.dict())
            except Exception:
                reasons.append("match_evaluation_error")
                continue
            if not matched or selected and snapshot["bundle"]["correlation_overlap"] == "first_match":
                continue
            selected = True
            lifecycle = next(item for item in snapshot["bundle"]["lifecycle"] if item["id"] == rule["lifecycle_ref"])
            version = digest({"rule": rule, "lifecycle": lifecycle, "overlap": snapshot["bundle"]["correlation_overlap"]})
            keys, missing = grouping_keys(incident["tenant_id"], event, rule, version)
            if missing or not keys:
                reasons.append("missing_normalized_identity")
            for key in keys:
                group = groups.setdefault(key["key"], {"key": key, "rule": rule, "lifecycle": lifecycle,
                    "version": version, "members": []})
                from keep.api.core.incident_lifecycle import event_time
                at = event_time(Alert(**row), lifecycle)
                if at is None:
                    reasons.append("invalid_event_time")
                else:
                    group["members"].append({"fingerprint": row["fingerprint"], "id": row["id"], "at": at.isoformat(),
                        "first_at": at.isoformat(), "status": event.status, "episode": 1,
                        "normalized": serial(event.normalized), "normalization": serial(event.normalization)})
        if not selected:
            reasons.append("no_matching_rule")
    return groups, sorted(set(reasons))


def canonical_transitions(data):
    result = []
    for row in data["audit"]:
        match = re.match(r"Incident status changed from (firing|acknowledged|resolved|merged|deleted) to (firing|acknowledged|resolved|merged|deleted)(?:;|$)", row["description"] or "")
        if match:
            result.append({"audit_id": row["id"], "from_status": match[1], "to_status": match[2],
                "recorded_at": row["timestamp"], "source": "keep.audit", "actor_provenance": row["user_id"]})
    return result


class LegacyIncidentMigration:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    def preview(self, session, exported, candidate, manifest, *, now=None):
        now = timestamp(now or datetime.now(timezone.utc))
        validate_shape("LegacyIncidentMigration", manifest, "migration")
        plan = defaults(manifest, SCHEMA["$defs"]["LegacyIncidentMigration"])
        require(plan["tenant_id"] == self.tenant_id == exported["tenant_id"], "migration.tenant_id", "trusted tenant mismatch")
        require(exported["digest"] == digest({key: item for key, item in exported.items() if key != "digest"}),
                "migration.source", "export digest mismatch")
        require(plan["source_id"] == exported["source_id"] and plan["source_digest"] == exported["digest"],
                "migration.source", "source changed; export and review again")
        require(plan["candidate_digest"] == candidate.digest, "migration.candidate", "candidate changed")
        roots = {item["root_id"]: item for item in plan["roots"]}
        require(len(roots) == len(plan["roots"]) and not set(roots) - set(exported["roots"]), "migration.roots", "duplicate or unknown root")
        rules = {item["legacy_rule_id"]: item["correlation_ref"] for item in plan["rules"]}
        destinations = {item["legacy_id"]: item["destination_ref"] for item in plan["destinations"]}
        require(len(rules) == len(plan["rules"]) and len(destinations) == len(plan["destinations"]), "migration.mappings", "duplicate mapping")
        snapshot = candidate_snapshot(candidate)
        teams = {item["id"] for item in candidate.documents["access"]["teams"]} | {None}
        require(set(plan["zones"].values()) <= teams, "migration.zones", "unknown team")
        require(set(rules.values()) <= {item["id"] for item in candidate.bundle["correlation"]}, "migration.rules", "unknown correlation rule")
        require(set(destinations.values()) <= {item["id"] for item in candidate.bundle["destinations"]}, "migration.destinations", "unknown destination")
        items, claimed = [], {}
        for root_id, root in sorted(exported["roots"].items()):
            action, state = roots.get(root_id), root["state"]
            reasons, groups, by_member = list(root["errors"]), {}, {}
            owners = set()
            for identifier in root["members"]:
                source = exported["incidents"].get(identifier)
                if source is None:
                    reasons.append("missing_or_foreign_incident")
                    continue
                owners.add(source["data"]["incident"]["team_id"])
                calculated, errors = evaluated_groups(source, snapshot)
                groups.update(calculated)
                by_member[identifier] = calculated
                reasons.extend(errors)
            if len(owners) != 1 or not owners <= teams:
                reasons.append("missing_or_foreign_owner")
            if not action:
                reasons.append("unreviewed_root")
            elif action["incident_id"] not in root["members"] or owners != {action["team_id"]}:
                reasons.append("target_scope_mismatch")
            zone = state.get("mm_zone")
            if zone and (zone not in plan["zones"] or owners != {plan["zones"][zone]}):
                reasons.append("unmapped_or_foreign_zone")
            if action and action["mode"] == "adopt":
                for identifier, calculated in by_member.items():
                    old = exported["incidents"][identifier]["data"]["incident"]
                    if rules.get(old["rule_id"]) not in {group["rule"]["id"] for group in calculated.values()}:
                        reasons.append("unmapped_legacy_rule")
                target = exported["incidents"].get(action["incident_id"])
                if len(groups) != 1:
                    reasons.append("split_required" if len(groups) > 1 else "missing_group")
                if target:
                    incident = target["data"]["incident"]
                    groups.update(by_member.get(action["incident_id"], {}))
                    if incident["correlation_context"] or incident["automation_context"]:
                        reasons.append("already_managed")
                    if incident["lifecycle_context"] and incident["lifecycle_context"].get("team_id") != incident["team_id"]:
                        reasons.append("existing_lifecycle_scope_mismatch")
                    if incident["status"] in {"merged", "deleted"}:
                        reasons.append("target_retired")
                    if rules.get(incident["rule_id"]) != next(iter(groups.values()), {}).get("rule", {}).get("id"):
                        reasons.append("unmapped_legacy_rule")
                    for member in root["members"]:
                        other = exported["incidents"].get(member)
                        if member != action["incident_id"] and other and other["data"]["incident"]["status"] not in {"resolved", "merged", "deleted"}:
                            reasons.append("multiple_active_incidents_require_review")
                for key in groups:
                    binding = session.get(IncidentCorrelationGroup, key)
                    if binding and binding.incident_id is not None:
                        reasons.append("group_already_owned")
                    if key in claimed:
                        reasons.append("planned_group_collision")
                        claimed[key]["reasons"].append("planned_group_collision")
            elif action and (action["post_policy"] == "adopt" or action["snooze"] == "import"):
                reasons.append("history_only_cannot_adopt_post_or_snooze")
            if action and action["mode"] == "retain_history":
                reasons = [reason for reason in reasons if reason not in {
                    "missing_history", "missing_normalized_identity", "no_matching_rule", "match_evaluation_error", "invalid_event_time", "invalid_alert_payload"}]
            destination_id = destinations.get(state.get("mm_channel"))
            if action and action["post_policy"] == "create" and state.get("mm_channel"):
                destination = next((item for item in candidate.bundle["destinations"] if item["id"] == destination_id), None)
                if not destination or destination["team_id"] != action["team_id"]:
                    reasons.append("unmapped_or_foreign_destination")
            if action and action["post_policy"] == "adopt":
                destination = next((item for item in candidate.bundle["destinations"] if item["id"] == destination_id), None)
                transport = next((item for item in candidate.bundle["transports"] if destination and item["id"] == destination["transport_ref"]), None)
                receipt = action.get("verified_post", {})
                if (len(root["members"]) != 1 or not destination or destination["team_id"] != action["team_id"]
                        or not transport or transport["kind"] != "mattermost" or not transport["capabilities"]["update"]
                        or destination["options"].get("channel_id") != state.get("mm_channel")):
                    reasons.append("post_scope_or_adapter_mismatch")
                if (not receipt.get("exists") or receipt.get("external_id") != state.get("mm_post_id")
                        or receipt.get("destination_external_id") != state.get("mm_channel")):
                    reasons.append("post_missing_or_unverified")
                elif not timedelta(0) <= now - timestamp(receipt["checked_at"]) <= timedelta(seconds=plan["post_receipt_max_age_seconds"]):
                    reasons.append("post_receipt_expired")
            until, active_snooze = None, False
            try:
                until = timestamp(state["snooze_until"]) if state.get("snooze_until") else None
                active_snooze = bool(until and until > now)
            except (ValueError, TypeError, AttributeError):
                reasons.append("invalid_snooze_time")
            if action and action["mode"] == "adopt" and active_snooze and action["snooze"] != "import":
                reasons.append("active_legacy_snooze_requires_import")
            topology = "one_to_one" if len(root["members"]) == len(groups) == 1 else "many_to_one" if len(groups) == 1 else "one_to_many" if len(root["members"]) == 1 and len(groups) > 1 else "many_to_many"
            item = {"root_id": root_id, "members": root["members"], "topology": topology,
                "group_ids": sorted(groups), "groups": groups, "action": action,
                "reasons": reasons, "destination_id": destination_id,
                "snooze_active": active_snooze, "snooze_until": until.isoformat() if until else None,
                "post_plan": "retire_legacy_buttons_then_" + (action["post_policy"] if action else "review"),
                "future_plan": "retain IDs/history; choose a survivor only after reviewing active incidents" if topology == "many_to_one" else
                    "retain IDs/history; split into independent canonical incidents and retire shared buttons" if len(groups) > 1 else "review missing identity/owner" if not groups else "adopt existing incident ID"}
            items.append(item)
            if action and action["mode"] == "adopt":
                for key in groups:
                    claimed[key] = item
        for item in items:
            item["reasons"] = sorted(set(item["reasons"]))
            item["status"] = ("unrecoverable" if set(item["reasons"]) & {"missing_or_foreign_incident", "missing_or_foreign_owner", "missing_root"}
                              else "ambiguous" if item["reasons"] else "ready")
        report = {"schema_version": 1, "tenant_id": self.tenant_id, "migration_id": plan["id"],
            "source_id": plan["source_id"], "source_digest": exported["digest"], "candidate_digest": candidate.digest,
            "plan_digest": digest(plan), "items": items,
            "summary": {status: sum(item["status"] == status for item in items) for status in ("ready", "ambiguous", "unrecoverable")}}
        report["preview_digest"] = digest(report)
        return report

    def apply(self, session, exported, candidate, manifest, *, expected_preview_digest, actor="iac-cli", now=None):
        now = timestamp(now or datetime.now(timezone.utc))
        validate_shape("LegacyIncidentMigration", manifest, "migration")
        plan = defaults(manifest, SCHEMA["$defs"]["LegacyIncidentMigration"])
        require(plan["tenant_id"] == self.tenant_id, "migration.tenant", "trusted tenant mismatch")
        require(exported["tenant_id"] == self.tenant_id and exported["source_id"] == plan["source_id"]
                and exported["digest"] == plan["source_digest"] == digest({key: value for key, value in exported.items() if key != "digest"})
                and candidate.digest == plan["candidate_digest"], "migration.source", "source or candidate changed")
        require(isinstance(actor, str) and 0 < len(actor) <= 256, "migration.actor", "invalid service actor")
        try:
            if session.get_bind().dialect.name == "sqlite":
                connection = session.connection()
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
            require(session.exec(select(Tenant).where(Tenant.id == self.tenant_id).with_for_update()).first() is not None,
                    "migration.tenant", "unknown tenant")
            existing = [session.get(LegacyIncidentImport, (self.tenant_id, plan["source_id"], item["root_id"])) for item in plan["roots"]]
            if any(existing):
                require(all(existing) and all(row.plan_digest == digest(plan) and row.migration_id == plan["id"]
                        and row.source_digest == exported["digest"] and row.candidate_digest == candidate.digest
                        and row.provenance["reviewed_preview_digest"] == expected_preview_digest for row in existing),
                        "migration.replay", "source ID already imported with a different plan")
                session.rollback()
                return {"result": "noop", "items": [row.result for row in existing]}
            active = session.exec(select(IncidentConfiguration).where(IncidentConfiguration.tenant_id == self.tenant_id).with_for_update()).first()
            require(active is not None and active.digest == candidate.digest, "migration.configuration", "apply the reviewed candidate first")
            report = self.preview(session, exported, candidate, manifest, now=now)
            require(report["preview_digest"] == expected_preview_digest, "migration.preview", "preview changed; review again")
            selected = {item["root_id"] for item in plan["roots"]}
            items = [item for item in report["items"] if item["root_id"] in selected]
            require(all(item["status"] == "ready" for item in items), "migration.targets", "selected roots are not ready")
            for item in items:
                action = item["action"]
                owners = ownership(active.snapshot, action["team_id"])
                require(owners["domain"] == owners["notifications"] == "disabled" and owners["legacy_snooze"] == "disabled",
                        "migration.runtime_ownership", "import requires explicit paused domain/sender and retired legacy snooze declaration")
                for identifier in item["members"]:
                    fresh = incident_snapshot(session, self.tenant_id, identifier)
                    require(fresh is not None and fresh["digest"] == exported["incidents"][identifier]["digest"],
                            "migration.source", "Keep operator/history state changed; export and review again")
            result = []
            for item in items:
                action = item["action"]
                incident = session.exec(select(Incident).where(Incident.id == UUID(action["incident_id"]),
                    Incident.tenant_id == self.tenant_id).with_for_update()).one()
                imported = self._import(session, incident, item, active.snapshot, plan, now)
                provenance = {"bridge": exported["roots"][item["root_id"]],
                    "keep": {identifier: exported["incidents"][identifier] for identifier in item["members"]},
                    "domain_transitions": {identifier: canonical_transitions(exported["incidents"][identifier]["data"]) for identifier in item["members"]},
                    "legacy_counters_authoritative": False, "legacy_actor_verified": False, "history_complete": False,
                    "reviewed_preview_digest": expected_preview_digest}
                if imported["binding_id"]:
                    from keep.api.models.db.incident_notification import IncidentNotificationBinding
                    session.flush()
                    provenance["binding"] = serial(session.get(IncidentNotificationBinding, imported["binding_id"]).dict())
                session.add(LegacyIncidentImport(tenant_id=self.tenant_id, source_id=plan["source_id"], root_id=item["root_id"],
                    migration_id=plan["id"], plan_digest=digest(plan), source_digest=exported["digest"], candidate_digest=candidate.digest,
                    imported_by=actor, imported_at=now, provenance=provenance, result=imported))
                from keep.api.core.db import add_audit
                from keep.api.models.action_type import ActionType
                add_audit(self.tenant_id, str(incident.id), "keep:legacy-incident-migration", ActionType.INCIDENT_ENRICH,
                    "Legacy incident state imported; source=" + plan["source_id"] + "; migration=" + plan["id"] +
                    "; legacy operator is unverified provenance; history_complete=false", session, commit=False)
                result.append(imported)
            session.commit()
            return {"result": "imported", "items": result}
        except Exception:
            session.rollback()
            raise

    def _import(self, session, incident, item, snapshot, plan, now):
        action, silence_id, binding_key = item["action"], None, None
        if action["mode"] == "adopt":
            from keep.api.core import incident_lifecycle as life
            from keep.api.core.incident_correlation import lock_group
            chosen = next(iter(item["groups"].values()))
            group = lock_group(session, self.tenant_id, incident.team_id, chosen["rule"], chosen["version"], chosen["key"]["key"])
            require(group.incident_id is None, "migration.group", "group is already owned")
            opened = life.utc(incident.start_time or incident.creation_time)
            window_opened = now if action["window_start"] == "cutover_time" else opened
            group.incident_id, group.opened_at = incident.id, window_opened
            episode = (incident.lifecycle_context or {}).get("episode", 1)
            cursors = {row["fingerprint"]: {key: value for key, value in row.items() if key not in {"fingerprint", "normalized", "normalization"}}
                       for row in chosen["members"]}
            for cursor in cursors.values():
                cursor["episode"] = episode
            group.lifecycle_state = {"members": cursors, "phase": life.phase(incident.status), "transitions": [],
                "watermark": max(row["at"] for row in chosen["members"]), "migration_history_complete": False}
            incident.rule_fingerprint, incident.rule_id = group.id, None
            incident.correlation_context = {"team_id": incident.team_id, "rule_id": chosen["rule"]["id"],
                "rule_version": chosen["version"], "config_digest": snapshot["digest"], "policy": copy.deepcopy(chosen["rule"]),
                "lifecycle": copy.deepcopy(chosen["lifecycle"]), "overlap": snapshot["bundle"]["correlation_overlap"],
                "group_values": chosen["key"]["values"], "fallback": chosen["key"]["fallback"],
                "window_start": window_opened.isoformat(), "window_end": (window_opened + timedelta(seconds=chosen["rule"]["window_seconds"])).isoformat(),
                "migration": {"source_id": plan["source_id"], "root_id": item["root_id"], "history_complete": False}}
            life.initialize(incident, group, chosen["lifecycle"], opened)
            incident.lifecycle_context = {**incident.lifecycle_context, "team_id": incident.team_id,
                "group_id": group.id, "policy_version": chosen["version"], "clock": chosen["lifecycle"]["clock"]}
            if incident.status == "resolved" and incident.end_time:
                incident.lifecycle_context = {**incident.lifecycle_context, "resolved_at": life.utc(incident.end_time).isoformat()}
            from keep.api.core.event_normalization import refresh_incident_presentation
            refresh_incident_presentation(self.tenant_id, incident, session, snapshot=snapshot,
                events=[{"normalized": row["normalized"], "normalization": row["normalization"]} for row in chosen["members"]])
            from keep.api.core.incident_automation import _new_state, selected_policy
            policy = selected_policy(incident, snapshot)
            if policy and incident.status in {"firing", "acknowledged"}:
                origin = now if action["automation_start"] == "cutover_time" else opened
                incident.automation_context = _new_state(incident, policy, snapshot, episode, origin, origin)
                if incident.status == "acknowledged" and incident.status in policy["stop_on"]:
                    incident.automation_context["stopped_reason"] = "acknowledged"
            session.add(group)
            session.add(incident)
            if item["snooze_active"] and action["snooze"] == "import":
                from keep.api.bl.silences_migration_bl import SilencesMigrationBL
                silence_id = SilencesMigrationBL(session)._insert(tenant_id=self.tenant_id, team_id=incident.team_id,
                    selector={"kind": "incident", "incident_ids": [str(incident.id)]}, starts=now, ends=timestamp(item["snooze_until"]),
                    comment="Imported bridge snooze; original operator is unverified legacy provenance",
                    correlation="bridge:" + digest([plan["source_id"], item["root_id"]]), legacy_author=None, now=now)
            if action["post_policy"] == "adopt":
                from keep.api.core.incident_notifications import binding_id
                from keep.api.models.db.incident_notification import IncidentNotificationBinding
                destination = next(row for row in snapshot["bundle"]["destinations"] if row["id"] == item["destination_id"])
                transport = next(row for row in snapshot["bundle"]["transports"] if row["id"] == destination["transport_ref"])
                binding_key = binding_id(incident, destination, transport)
                require(session.get(IncidentNotificationBinding, binding_key) is None, "migration.binding", "binding already exists")
                session.add(IncidentNotificationBinding(id=binding_key, tenant_id=self.tenant_id, team_id=incident.team_id,
                    incident_id=incident.id, destination_id=destination["id"], transport_id=transport["id"],
                    external_id=action["verified_post"]["external_id"]))
        return {"root_id": item["root_id"], "incident_id": str(incident.id), "mode": action["mode"],
                "silence_id": str(silence_id) if silence_id else None, "binding_id": binding_key}

    def receipts(self, session, source_id):
        rows = session.exec(select(LegacyIncidentImport).where(LegacyIncidentImport.tenant_id == self.tenant_id,
            LegacyIncidentImport.source_id == source_id).order_by(LegacyIncidentImport.root_id)).all()
        result = {"schema_version": 1, "tenant_id": self.tenant_id, "source_id": source_id,
                  "imports": [serial(row.dict()) for row in rows]}
        result["digest"] = digest(result)
        return result

    def restore_bindings(self, session, source_id, *, expected_snapshot_digest):
        """Rebuild missing mappings only, from reviewed immutable DB receipts."""
        from keep.api.models.db.incident_notification import IncidentNotificationBinding
        from keep.api.core.incident_notifications import binding_id
        try:
            if session.get_bind().dialect.name == "sqlite":
                connection = session.connection()
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
            session.exec(select(Tenant).where(Tenant.id == self.tenant_id).with_for_update()).one()
            active = session.exec(select(IncidentConfiguration).where(IncidentConfiguration.tenant_id == self.tenant_id).with_for_update()).one()
            saved = self.receipts(session, source_id)
            require(saved["digest"] == expected_snapshot_digest, "migration.restore", "receipt snapshot changed")
            created = 0
            for receipt in saved["imports"]:
                fields = receipt["provenance"].get("binding")
                if not fields:
                    continue
                incident = session.exec(select(Incident).where(Incident.id == UUID(fields["incident_id"]),
                    Incident.tenant_id == self.tenant_id).with_for_update()).one()
                owners = ownership(active.snapshot, incident.team_id)
                require(owners["domain"] == owners["notifications"] == "disabled", "migration.restore", "pause domain/sender before binding restore")
                destination = next((item for item in active.snapshot["bundle"]["destinations"] if item["id"] == fields["destination_id"]), None)
                transport = next((item for item in active.snapshot["bundle"]["transports"] if item["id"] == fields["transport_id"]), None)
                require(incident.team_id == fields["team_id"] and destination and destination["team_id"] == incident.team_id
                        and transport and destination["transport_ref"] == transport["id"]
                        and binding_id(incident, destination, transport) == fields["id"], "migration.restore", "binding scope/configuration changed")
                current = session.get(IncidentNotificationBinding, fields["id"])
                require(current is None or serial(current.dict()) == fields, "migration.restore", "binding changed; never overwrite a live mapping")
                if current is None:
                    session.add(IncidentNotificationBinding(**fields))
                    created += 1
            session.commit()
            return {"result": "restored", "created": created}
        except Exception:
            session.rollback()
            raise
