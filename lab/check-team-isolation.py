"""Check the desired team policy against lab data in a separate read-only process.

Run inside keep-backend with KEEP_TEAMS_CONFIG and KEEP_LAB_MEMBERSHIPS set.
This does not change the server's policy, Keycloak or the database.
"""

import json
import os

import yaml
from fastapi import HTTPException
from sqlalchemy import event

import keep.api.routes.alerts  # Register database models.
from keep.api.core.db import engine, get_last_alerts
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.incidents import get_last_incidents_by_cel
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.team_access import (
    require_alert_access,
    require_incident_access,
    visible_team_ids,
)
from keep.identitymanager.team_policy import get_team_policy


@event.listens_for(engine, "begin")
def read_only_transaction(connection):
    if engine.dialect.name == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")


def expect_denied(action, status):
    try:
        action()
    except HTTPException as error:
        assert error.status_code == status, (error.status_code, status)
    else:
        raise AssertionError(f"Expected access to be denied with {status}")


get_team_policy.cache_clear()
policy = get_team_policy()
assert policy and policy.visibility == "team", "Use the desired isolation policy"
memberships = yaml.safe_load(os.environ["KEEP_LAB_MEMBERSHIPS"])["users"]
all_alerts = get_last_alerts(SINGLE_TENANT_UUID)
all_incidents, _ = get_last_incidents_by_cel(
    SINGLE_TENANT_UUID, is_candidate=False, limit=1000,
)
assert all_alerts and all_incidents, "The lab needs alert and incident fixtures"

for username, groups in memberships.items():
    roles = policy.roles_for_groups(set(groups))
    role = next(item for item in ("admin", "responder", "viewer", "noc") if item in roles)
    teams = policy.teams_for_groups(set(groups))
    entity = AuthenticatedEntity(
        tenant_id=SINGLE_TENANT_UUID, email=f"{username}@lab.example.test",
        role=role, teams=teams, visible_teams=policy.visible_teams(set(teams)),
    )
    allowed = visible_team_ids(entity)
    alerts = get_last_alerts(SINGLE_TENANT_UUID, allowed_team_ids=allowed)
    incidents, count = get_last_incidents_by_cel(
        SINGLE_TENANT_UUID, is_candidate=False, limit=1000, allowed_team_ids=allowed,
    )
    assert alerts and incidents, f"No visible fixtures for {username}"
    if allowed is not None:
        assert all(item.team_id in allowed for item in alerts)
        assert all(item.team_id in allowed for item in incidents)
        foreign_alert = next(item for item in all_alerts if item.team_id not in allowed)
        foreign_incident = next(item for item in all_incidents if item.team_id not in allowed)
        expect_denied(lambda: require_alert_access(entity, foreign_alert.fingerprint), 404)
        expect_denied(lambda: require_incident_access(entity, foreign_incident.id), 404)
        if role == "viewer":
            expect_denied(lambda: require_alert_access(entity, alerts[0].fingerprint, for_write=True), 403)
            expect_denied(lambda: require_incident_access(entity, incidents[0].id, for_write=True), 403)
        else:
            require_alert_access(entity, alerts[0].fingerprint, for_write=True)
            require_incident_access(entity, incidents[0].id, for_write=True)
    print(json.dumps({
        "user": username, "role": role,
        "visible_teams": sorted(allowed) if allowed is not None else None,
        "alerts": len(alerts), "incidents": count, "access_checks": "passed",
    }))
