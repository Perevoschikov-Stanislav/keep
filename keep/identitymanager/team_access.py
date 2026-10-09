"""Team visibility is enforced independently from role scopes."""

from collections import defaultdict

from fastapi import HTTPException

from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.team_policy import get_team_policy, is_team_scoping_active


def has_global_access(entity: AuthenticatedEntity) -> bool:
    if hasattr(entity, "delegated_visible_teams"):
        return False
    return not is_team_scoping_active() or entity.role == "admin"


def writable_team_ids(entity: AuthenticatedEntity) -> frozenset[str] | None:
    if hasattr(entity, "delegated_writable_teams"):
        return entity.delegated_writable_teams
    if has_global_access(entity):
        return None
    if entity.role not in {"responder", "noc"}:
        return frozenset()
    return frozenset(getattr(entity, "teams", ()))


def visible_team_ids(entity: AuthenticatedEntity) -> frozenset[str] | None:
    """None means unrestricted; an empty set means no data is visible."""
    if hasattr(entity, "delegated_visible_teams"):
        return entity.delegated_visible_teams
    if has_global_access(entity):
        return None
    policy = get_team_policy()
    if policy and policy.visibility == "all":
        return None
    return frozenset(getattr(entity, "visible_teams", ()))


def alert_history_visible_clause(
    tenant_id: str, fingerprint, allowed_team_ids: frozenset[str]
):
    """A shared fingerprint is hidden if any event belongs to another team."""
    from sqlalchemy import exists, or_, select
    from sqlalchemy.orm import aliased

    from keep.api.models.db.alert import Alert

    history = aliased(Alert)
    return ~exists(
        select(history.id).where(
            history.tenant_id == tenant_id,
            history.fingerprint == fingerprint,
            or_(
                history.team_id.is_(None),
                ~history.team_id.in_(allowed_team_ids),
            ),
        )
    )


def incident_history_visible_clause(tenant_id: str, incident_id, team_id):
    """A linked incident is hidden if its alert history has another owner."""
    from sqlalchemy import and_, exists, or_, select
    from sqlalchemy.orm import aliased

    from keep.api.models.db.alert import Alert, LastAlertToIncident

    history = aliased(Alert)
    return ~exists(
        select(history.id)
        .select_from(LastAlertToIncident)
        .join(
            history,
            and_(
                history.tenant_id == LastAlertToIncident.tenant_id,
                history.fingerprint == LastAlertToIncident.fingerprint,
            ),
        )
        .where(
            LastAlertToIncident.tenant_id == tenant_id,
            LastAlertToIncident.incident_id == incident_id,
            or_(history.team_id.is_(None), history.team_id != team_id),
        )
    )


def require_team_access(
    entity: AuthenticatedEntity, team_id: str | None, *, for_write: bool = False
) -> None:
    allowed = visible_team_ids(entity)
    if allowed is not None and (team_id not in allowed):
        raise HTTPException(status_code=404, detail="Not found")
    writable = writable_team_ids(entity)
    if for_write and writable is not None and (team_id not in writable):
        raise HTTPException(
            status_code=403,
            detail="Read only: you can only modify data belonging to your own team",
        )


def require_incident_access(entity: AuthenticatedEntity, incident_id, *, for_write=False):
    from sqlmodel import Session, select

    from keep.api.core.db import engine, get_incident_by_id
    from keep.api.models.db.alert import Alert, LastAlertToIncident

    incident = get_incident_by_id(entity.tenant_id, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    require_team_access(entity, incident.team_id, for_write=for_write)
    if not has_global_access(entity) and (for_write or visible_team_ids(entity) is not None):
        with Session(engine) as session:
            linked_teams = session.exec(
                select(Alert.team_id)
                .select_from(LastAlertToIncident)
                .join(
                    Alert,
                    (LastAlertToIncident.tenant_id == Alert.tenant_id)
                    & (LastAlertToIncident.fingerprint == Alert.fingerprint),
                )
                .where(LastAlertToIncident.tenant_id == entity.tenant_id)
                .where(LastAlertToIncident.incident_id == incident.id)
                .distinct()
            ).all()
        if any(team_id != incident.team_id for team_id in linked_teams):
            if for_write and visible_team_ids(entity) is None:
                raise HTTPException(
                    status_code=403,
                    detail="Read only: this incident contains alerts from multiple teams",
                )
            raise HTTPException(status_code=404, detail="Not found")
    return incident


def require_alert_access(entity: AuthenticatedEntity, fingerprint: str, *, for_write=False) -> None:
    allowed = visible_team_ids(entity)
    if allowed is None and (not for_write or writable_team_ids(entity) is None):
        return
    from sqlmodel import Session, select

    from keep.api.core.db import engine
    from keep.api.models.db.alert import Alert

    with Session(engine) as session:
        teams = session.exec(
            select(Alert.team_id)
            .where(Alert.tenant_id == entity.tenant_id)
            .where(Alert.fingerprint == fingerprint)
            .distinct()
        ).all()
    if not teams:
        raise HTTPException(status_code=404, detail="Alert not found")
    for team_id in teams:
        require_team_access(entity, team_id, for_write=for_write)


def accessible_alert_fingerprints(
    entity: AuthenticatedEntity, fingerprints: set[str]
) -> set[str]:
    allowed = visible_team_ids(entity)
    if allowed is None:
        return fingerprints
    if not fingerprints:
        return set()
    from sqlmodel import Session, select

    from keep.api.core.db import engine
    from keep.api.models.db.alert import Alert

    with Session(engine) as session:
        rows = session.exec(
            select(Alert.fingerprint, Alert.team_id)
            .where(Alert.tenant_id == entity.tenant_id)
            .where(Alert.fingerprint.in_(fingerprints))
            .distinct()
        ).all()
    teams_by_fingerprint = defaultdict(set)
    for fingerprint, team_id in rows:
        teams_by_fingerprint[fingerprint].add(team_id)
    return {
        fingerprint
        for fingerprint, teams in teams_by_fingerprint.items()
        if teams <= allowed
    }
