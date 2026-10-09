"""Reviewed, explicit transition from legacy dismiss and tenant maintenance rules."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from keep.api.bl.silences_bl import compile_filter, resource
from keep.api.models.db.alert import Alert, AlertEnrichment
from keep.api.models.db.maintenance_window import MaintenanceWindowRule
from keep.api.models.db.silence import Silence, SilenceEvent
from keep.api.models.db.tenant import Tenant
from keep.api.models.silence import SilenceActor, utc_now, utc_string
from keep.identitymanager.team_policy import get_team_policy


def _parse_utc_datetime(value: Any):
    if value is None or value == "" or value == "forever":
        return None
    if isinstance(value, str):
        if "T" not in value and " " not in value:
            raise ValueError("Invalid legacy timestamp")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Invalid legacy timestamp")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _dismissed(value):
    return (value is True or type(value) is int and value == 1
            or isinstance(value, str) and value.lower() in {"true", "1"})


class SilencesMigrationBL:
    def __init__(self, session: Session):
        self.session = session

    def inventory(self, tenant_id=None):
        now = utc_now()
        report = {
            "timestamp": utc_string(now), "tenant_id": tenant_id,
            "dismissals": {"total": 0, "active_migratable": 0, "expired": 0,
                           "indefinite": 0, "with_suppressed_override": 0,
                           "with_disposable_flags": 0, "items": []},
            "maintenance_rules": {"total": 0, "active_migratable": 0,
                                  "expired_or_disabled": 0, "unassigned_team_rules": 0,
                                  "items": []},
            "admin_action_required": [],
        }
        policy = get_team_policy()
        query = select(AlertEnrichment)
        if tenant_id is not None:
            query = query.where(AlertEnrichment.tenant_id == tenant_id)
        # Decode JSON in Python: PostgreSQL json_extract text cannot be compared to integer 1.
        for enrichment in self.session.exec(query).all():
            data = enrichment.enrichments or {}
            if data.get("status") == "suppressed":
                report["admin_action_required"].append({
                    "type": "dismiss_status_provenance_required", "tenant_id": enrichment.tenant_id,
                    "fingerprint": enrichment.alert_fingerprint,
                    "message": "Status override has no reliable Dismiss provenance; review it explicitly",
                })
            if not _dismissed(data.get("dismissed")):
                continue
            owners = self.session.exec(select(Alert.team_id).where(
                Alert.tenant_id == enrichment.tenant_id,
                Alert.fingerprint == enrichment.alert_fingerprint,
            ).distinct()).all()
            owner_confirmed = len(owners) == 1 and (
                owners[0] is None or policy is not None and owners[0] in policy.teams
            )
            invalid_time = False
            try:
                ends = _parse_utc_datetime(data.get("dismissUntil"))
            except (ValueError, TypeError):
                ends, invalid_time = None, True
            indefinite = not invalid_time and ends is None
            expired = not invalid_time and ends is not None and ends <= now
            active = not invalid_time and not expired
            item = {
                "tenant_id": enrichment.tenant_id, "fingerprint": enrichment.alert_fingerprint,
                "active": active, "migratable": active and owner_confirmed,
                "is_indefinite": indefinite, "ends_at": utc_string(ends),
                "invalid_time": invalid_time, "owner_confirmed": owner_confirmed,
                "detected_team_id": owners[0] if owner_confirmed else None,
                "canonical_owners": sorted(owners, key=lambda owner: owner or ""),
                "has_suppressed_override": data.get("status") == "suppressed",
                "has_disposable_flags": any(key.startswith("disposable_dismiss") for key in data),
                "note": data.get("note"), "legacy_author": data.get("dismissed_by"),
                "source_hash": _hash([enrichment.tenant_id, enrichment.alert_fingerprint, data,
                                      sorted(owners, key=lambda owner: owner or "")]),
            }
            stats = report["dismissals"]
            stats["total"] += 1
            stats["active_migratable"] += int(item["migratable"])
            stats["expired"] += int(expired)
            stats["indefinite"] += int(indefinite)
            stats["with_suppressed_override"] += int(item["has_suppressed_override"])
            stats["with_disposable_flags"] += int(item["has_disposable_flags"])
            stats["items"].append(item)
            for condition, reason in (
                (invalid_time, "invalid_dismiss_time"),
                (not owner_confirmed, "ambiguous_dismiss_owner"),
            ):
                if condition:
                    report["admin_action_required"].append({
                        "type": reason, "tenant_id": enrichment.tenant_id,
                        "fingerprint": enrichment.alert_fingerprint,
                        "message": "Explicit administrator review required; source data is retained",
                    })

        query = select(MaintenanceWindowRule)
        if tenant_id is not None:
            query = query.where(MaintenanceWindowRule.tenant_id == tenant_id)
        for rule in self.session.exec(query).all():
            starts = _parse_utc_datetime(rule.start_time)
            ends = _parse_utc_datetime(rule.end_time)
            active = rule.enabled and starts < ends and ends > now
            item = {
                "id": rule.id, "tenant_id": rule.tenant_id, "name": rule.name,
                "cel_query": rule.cel_query, "ignore_statuses": rule.ignore_statuses,
                "starts_at": utc_string(starts), "ends_at": utc_string(ends),
                "enabled": rule.enabled, "active": active, "created_by": rule.created_by,
                "team_id": None, "old_strategy": "suppress" if rule.suppress else "drop_event",
                "new_strategy": "notification_gate_silence",
            }
            item["source_hash"] = _hash(item)
            stats = report["maintenance_rules"]
            stats["total"] += 1
            stats["active_migratable"] += int(active)
            stats["expired_or_disabled"] += int(not active)
            stats["unassigned_team_rules"] += 1
            stats["items"].append(item)
            if active:
                report["admin_action_required"].append({
                    "type": "maintenance_team_unassigned", "tenant_id": rule.tenant_id,
                    "rule_id": rule.id, "rule_name": rule.name,
                    "message": "Review teams and CEL semantics; null covers unassigned objects only",
                })
        report["source_hash"] = _hash(sorted(
            item["source_hash"] for group in ("dismissals", "maintenance_rules")
            for item in report[group]["items"]
        ))
        return report

    def _insert(self, *, tenant_id, team_id, selector, starts, ends, comment, correlation, legacy_author, now):
        existing = self.session.exec(select(Silence).where(
            Silence.tenant_id == tenant_id, Silence.origin == "legacy-migration",
            Silence.team_id == team_id, Silence.correlation_id == correlation,
        )).first()
        if existing:
            return None
        # A legacy email is provenance, not a verified user subject.
        actor = SilenceActor(kind="service", subject="keep:legacy-silence-migration",
                             issuer=None, display_name="Legacy silence migration")
        rule = Silence(
            id=uuid4(), tenant_id=tenant_id, team_id=team_id, revision=1, selector=selector,
            starts_at=starts, ends_at=ends, comment=comment, created_by=actor.dict(),
            updated_by=actor.dict(), created_at=now, updated_at=now,
            origin="legacy-migration", correlation_id=correlation,
            last_event_state="scheduled" if starts > now else "active",
        )
        self.session.add(rule)
        event_id = uuid4()
        payload = {
            "schema_version": 1, "event_id": str(event_id), "event_type": "silence.created",
            "occurred_at": utc_string(now), "effective_at": utc_string(starts),
            "silence_id": str(rule.id), "revision": 1, "tenant_id": tenant_id,
            "team_id": team_id, "origin": "legacy-migration", "correlation_id": correlation,
            "client_request_id": None, "actor": actor.dict(),
            "reason": "Imported legacy rule" + (f"; legacy author: {legacy_author}" if legacy_author else ""),
            "resource": json.loads(resource(rule, now).json()),
        }
        from keep.api.bl.silences_delivery_bl import append_silence_event

        append_silence_event(self.session, SilenceEvent(event_id=event_id, tenant_id=tenant_id, team_id=team_id,
            silence_id=rule.id, revision=1, event_type="silence.created", occurred_at=now, payload=payload))
        self.session.flush()
        return rule.id

    def apply(self, tenant_id=None, include_maintenance=True, inventory_report=None, maintenance_teams=None):
        """Import a fresh canonical inventory; explicit assignments are required for maintenance."""
        try:
            if self.session.get_bind().dialect.name == "sqlite":
                connection = self.session.connection()
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
            tenants = select(Tenant).order_by(Tenant.id)
            if tenant_id is not None:
                tenants = tenants.where(Tenant.id == tenant_id)
            self.session.exec(tenants.with_for_update()).all()
            report = self.inventory(tenant_id)
            if inventory_report is not None and inventory_report.get("source_hash") != report["source_hash"]:
                raise ValueError("Legacy source changed since inventory; review a new dry-run")
            now = utc_now()
            plans = maintenance_teams or {}
            policy = get_team_policy()
            planned = []
            pending = 0
            known_rules = {(item["tenant_id"], str(item["id"]))
                           for item in report["maintenance_rules"]["items"]}
            if not isinstance(plans, dict) or any(
                not isinstance(assignments, dict) or any(
                    (tenant, str(identifier)) not in known_rules for identifier in assignments
                ) for tenant, assignments in plans.items()
            ):
                raise ValueError("Maintenance plan references an unknown tenant or rule")
            reviewed_rules = set()
            for item in report["maintenance_rules"]["items"] if include_maintenance else []:
                if not item["active"]:
                    continue
                assignment = plans.get(item["tenant_id"], {}).get(str(item["id"]))
                if assignment is None:
                    pending += 1
                    continue
                if not isinstance(assignment, dict) or set(assignment) != {"teams", "cel"}:
                    raise ValueError("Maintenance plan requires teams and reviewed CEL for every rule")
                teams, cel = assignment["teams"], assignment["cel"]
                if not isinstance(teams, list) or not teams or any(
                    team is not None and (not isinstance(team, str) or policy is None or team not in policy.teams)
                    for team in teams
                ):
                    raise ValueError("Maintenance plan must contain explicit configured teams (null means unassigned)")
                if not isinstance(cel, str) or not cel.strip():
                    raise ValueError("Maintenance plan must include a reviewed CEL filter")
                if item["ignore_statuses"]:
                    cel = f"({cel}) && !(status in {json.dumps(item['ignore_statuses'])})"
                compile_filter(cel)
                reviewed_rules.add((item["tenant_id"], item["id"]))
                for team in dict.fromkeys(teams):
                    planned.append((item, team, cel))
            result = {
                "timestamp": utc_string(now), "tenant_id": tenant_id,
                "dismissals_migrated": 0, "dismissals_skipped_already_present": 0,
                "dismissals_pending_review": sum(item["invalid_time"] or item["active"] and not item["migratable"]
                                                for item in report["dismissals"]["items"]),
                "suppressed_overrides_cleared": 0, "disposable_flags_cleaned": 0,
                "maintenance_rules_migrated": 0, "maintenance_rules_skipped_already_present": 0,
                "maintenance_rules_pending_review": pending, "created_silence_ids": [],
                "admin_action_required": [action for action in report["admin_action_required"]
                    if action["type"] != "maintenance_team_unassigned"
                    or (action["tenant_id"], action["rule_id"]) not in reviewed_rules],
            }
            for item in report["dismissals"]["items"]:
                if not item["migratable"]:
                    continue
                rule_id = self._insert(
                    tenant_id=item["tenant_id"], team_id=item["detected_team_id"],
                    selector={"kind": "alert", "fingerprints": [item["fingerprint"]]},
                    starts=now, ends=_parse_utc_datetime(item["ends_at"]),
                    comment=item["note"] or "Migrated legacy dismiss", correlation=f"legacy-dismiss:{item['fingerprint']}",
                    legacy_author=item["legacy_author"], now=now,
                )
                if rule_id:
                    result["dismissals_migrated"] += 1
                    result["created_silence_ids"].append(str(rule_id))
                else:
                    result["dismissals_skipped_already_present"] += 1
                enrichment = self.session.exec(select(AlertEnrichment).where(
                    AlertEnrichment.tenant_id == item["tenant_id"],
                    AlertEnrichment.alert_fingerprint == item["fingerprint"],
                ).with_for_update()).one()
                data = dict(enrichment.enrichments)
                for key in ("dismissed", "dismissUntil", "disposable_dismissed", "disposable_dismissUntil"):
                    if key.startswith("disposable_") and key in data:
                        result["disposable_flags_cleaned"] += 1
                    data.pop(key, None)
                # An old status override has no reliable association with Dismiss. Keep it for review.
                enrichment.enrichments = data
                self.session.add(enrichment)
            for item, team, cel in planned:
                rule_id = self._insert(
                    tenant_id=item["tenant_id"], team_id=team, selector={"kind": "filter", "cel": cel},
                    starts=_parse_utc_datetime(item["starts_at"]), ends=_parse_utc_datetime(item["ends_at"]),
                    comment=f"Migrated maintenance rule: {item['name']}",
                    correlation=f"legacy-maintenance:{item['id']}", legacy_author=item["created_by"], now=now,
                )
                key = "maintenance_rules_migrated" if rule_id else "maintenance_rules_skipped_already_present"
                result[key] += 1
                if rule_id:
                    result["created_silence_ids"].append(str(rule_id))
            self.session.commit()
            return result
        except Exception:
            self.session.rollback()
            raise
