from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory
from keep.identitymanager.team_access import visible_team_ids
from keep.identitymanager.team_policy import get_team_policy, is_team_scoping_active
from keep.api.core.incident_configuration import active_configuration

router = APIRouter()


class ConfiguredTeam(BaseModel):
    id: str
    groups: list[str]
    zones: list[str]
    visible_to: list[str]


class TeamsResponse(BaseModel):
    enabled: bool
    visibility: Literal["team", "all"] | None
    teams: list[ConfiguredTeam]
    configuration: dict | None = None


@router.get(
    "", response_model=TeamsResponse, response_model_exclude_none=True,
    description="Get visible teams from the configured policy",
)
def get_teams(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> TeamsResponse:
    policy = get_team_policy(authenticated_entity.tenant_id) if is_team_scoping_active() else None
    snapshot = active_configuration(authenticated_entity.tenant_id)
    configuration = None
    if snapshot:
        configuration = {"digest": snapshot["digest"], "generation": snapshot["generation"],
                         "revision": snapshot["bundle"]["revision"]}
        if authenticated_entity.role == "admin":
            from keep.api.bl.incident_provisioning import IncidentProvisioning
            current = IncidentProvisioning(authenticated_entity.tenant_id).status()
            configuration.update({"source": snapshot["source"], "applied_by": snapshot["applied_by"],
                                  "applied_at": snapshot["applied_at"], "result": "applied",
                                  "drift_count": len(current["drift"]) if current["active_digest"] == snapshot["digest"] else None})
    if policy is None:
        return TeamsResponse(enabled=False, visibility=None, teams=[], configuration=configuration)

    allowed = visible_team_ids(authenticated_entity)
    teams = [
        team for team in policy.teams.values()
        if allowed is None or team.id in allowed
    ]
    listed_ids = {team.id for team in teams}
    return TeamsResponse(
        enabled=True,
        configuration=configuration,
        visibility=policy.visibility,
        teams=[
            ConfiguredTeam(
                id=team.id,
                groups=sorted(team.groups),
                zones=sorted(team.zones),
                visible_to=sorted(team.visible_to & listed_ids),
            )
            for team in sorted(teams, key=lambda team: team.id)
        ],
    )
