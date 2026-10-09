"""Request-local CEL mappings for derived silence fields, before pagination/counts."""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, case, func, literal, or_
from sqlmodel import select

from keep.api.bl.silences_evaluator import SilenceEvaluator, chunks
from keep.api.core.cel_to_sql.ast_nodes import DataType
from keep.api.core.cel_to_sql.properties_metadata import FieldMappingConfiguration, PropertiesMetadata
from keep.api.models.db.alert import Alert, AlertEnrichment, LastAlert, LastAlertToIncident
from keep.api.models.db.helpers import NULL_FOR_DELETED_AT
from keep.api.models.db.incident import Incident
from keep.api.models.incident import IncidentDto
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts


def _sql_case(column, values, default, dialect):
    groups = defaultdict(list)
    for key, value in values.items():
        if value != default:
            groups[value].append(key)
    expression = case(*[(column.in_(keys), value) for value, keys in groups.items()], else_=default) if groups else literal(default)
    return str(expression.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def _candidate_alerts(session, tenant_id, rules, wants_dismissed, allowed_team_ids):
    """Exact rules and legacy dismiss use candidates; only CEL rules scan their teams."""
    fingerprints = set()
    incident_ids = set()
    filter_teams = set()
    for rule in rules:
        selector = rule.selector
        if selector["kind"] == "alert":
            fingerprints.update(selector["fingerprints"])
        elif selector["kind"] == "incident":
            incident_ids.update(selector["incident_ids"])
        else:
            filter_teams.add(rule.team_id)
    for batch in chunks(incident_ids):
        fingerprints.update(session.exec(select(LastAlertToIncident.fingerprint).where(
            LastAlertToIncident.tenant_id == tenant_id,
            LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
            LastAlertToIncident.incident_id.in_(batch),
        )).all())
    conditions = [Alert.fingerprint.in_(fingerprints), Alert.team_id.in_(filter_teams - {None})]
    if None in filter_teams:
        conditions.append(Alert.team_id.is_(None))
    query = select(Alert).join(LastAlert, and_(Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id))
    if wants_dismissed:
        query = query.outerjoin(AlertEnrichment, and_(
            AlertEnrichment.tenant_id == Alert.tenant_id, AlertEnrichment.alert_fingerprint == Alert.fingerprint,
        ))
        # Include candidates from both sources; the DTO applies enrichment precedence and expiry.
        conditions.extend(func.lower(source["dismissed"].as_string()).in_(["true", "1"])
                          for source in (Alert.event, AlertEnrichment.enrichments))
    query = query.where(Alert.tenant_id == tenant_id, or_(*conditions))
    if allowed_team_ids is not None:
        query = query.where(Alert.team_id.in_(allowed_team_ids))
    return session.exec(query).all()


def silence_properties(session, tenant_id, configurations, expressions, *, entity="alert", allowed_team_ids=None):
    """Use the same evaluator as effective; do not mutate global metadata across tenants."""
    if not any("dismissed" in (value or "") or "silence" in (value or "") for value in expressions):
        return PropertiesMetadata(configurations)
    evaluator = SilenceEvaluator(session, tenant_id)
    wants_dismissed = any("dismissed" in (value or "") for value in expressions)
    mappings = []
    alert_objects = _candidate_alerts(session, tenant_id, evaluator.rules, wants_dismissed, allowed_team_ids)
    alert_results = evaluator.alerts(alert_objects)
    # Incident queries can also filter a linked alert's derived fields.
    scopes = ["alert"] if entity == "alert" else ["incident", "alert"]
    for scope in scopes:
        if scope == "alert":
            objects = alert_objects
            results = alert_results
            dtos = convert_db_alerts_to_dto_alerts(objects, session=session, with_silences=False)
            legacy = {dto.fingerprint: dto.dismissed for dto in dtos}
            column = LastAlert.fingerprint
            keyed = {obj.fingerprint: results[obj.id] for obj in objects}
            prefixes = ["", "alert."] if entity == "alert" else ["alert."]
        else:
            # Only explicit incident targets or parents of silenced alerts can have coverage.
            incident_ids = {UUID(target) for rule in evaluator.rules if rule.selector["kind"] == "incident"
                            for target in rule.selector["incident_ids"]}
            if wants_dismissed:
                legacy_ids = session.exec(select(AlertEnrichment.alert_fingerprint).where(
                    AlertEnrichment.tenant_id == tenant_id,
                    func.lower(AlertEnrichment.enrichments["dismissed"].as_string()).in_(["true", "1"]),
                )).all()
                for target in legacy_ids:
                    try:
                        incident_ids.add(UUID(target))
                    except ValueError:
                        continue
            muted_fingerprints = [item.target.fingerprint for item in alert_results.values() if item.silenced]
            for batch in chunks(muted_fingerprints):
                incident_ids.update(session.exec(select(LastAlertToIncident.incident_id).where(
                    LastAlertToIncident.tenant_id == tenant_id,
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    LastAlertToIncident.fingerprint.in_(batch),
                )).all())
            objects = session.exec(select(Incident).where(Incident.tenant_id == tenant_id,
                Incident.id.in_(incident_ids),
                Incident.team_id.in_(allowed_team_ids) if allowed_team_ids is not None else True,
            )).all() if incident_ids else []
            by_id = {str(obj.id): obj for obj in objects}
            for batch in chunks(by_id):
                enrichments = session.exec(select(AlertEnrichment).where(
                    AlertEnrichment.tenant_id == tenant_id, AlertEnrichment.alert_fingerprint.in_(batch),
                )).all()
                for enrichment in enrichments:
                    by_id[enrichment.alert_fingerprint].set_enrichments(enrichment.enrichments)
            results = evaluator.incidents(objects)
            legacy = {obj.id: IncidentDto.from_db_incident(obj, with_silences=False).dismissed for obj in objects}
            column = Incident.id
            keyed = results
            prefixes = [""]
        derived = {
            "dismissed": ({key: bool(legacy.get(key)) or item.silenced for key, item in keyed.items()}, False, DataType.BOOLEAN),
            "silence.silenced": ({key: item.silenced for key, item in keyed.items()}, False, DataType.BOOLEAN),
            "silence.coverage": ({key: item.coverage for key, item in keyed.items()}, "none", DataType.STRING),
            "silence.silenced_until": ({key: datetime.fromisoformat(item.silenced_until[:-1]) if item.silenced_until else None
                                        for key, item in keyed.items()}, None, DataType.DATETIME),
        }
        for name, (values, default, data_type) in derived.items():
            sql = _sql_case(column, values, default, session.get_bind().dialect)
            mappings.extend(FieldMappingConfiguration(map_from_pattern=prefix + name,
                map_to=sql, data_type=data_type) for prefix in prefixes)
    overridden = {mapping.map_from_pattern for mapping in mappings}
    return PropertiesMetadata(mappings + [config for config in configurations if config.map_from_pattern not in overridden])
