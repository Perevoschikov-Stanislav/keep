"""IaC-owned group, team and visibility mapping for OAUTH2PROXY."""

import os
from dataclasses import dataclass
from functools import lru_cache

import yaml


@dataclass(frozen=True)
class Team:
    id: str
    groups: frozenset[str]
    zones: frozenset[str]
    visible_to: frozenset[str]


class TeamPolicy:
    def __init__(self, document: dict):
        if document.get("version") != 1:
            raise ValueError("Team policy version must be 1")
        self.visibility = document.get("visibility", "team")
        if self.visibility not in {"team", "all"}:
            raise ValueError("Team visibility must be team or all")
        self.incident_views = [{"id": "all", "name": "ALL", "cel": ""}]
        views = document.get("incident_views", [])
        if not isinstance(views, list):
            raise ValueError("Incident views must be a list")
        view_ids = {"all"}
        for view in views:
            if not isinstance(view, dict) or any(
                not isinstance(view.get(key), str) for key in ("id", "name", "cel")
            ) or not view["id"] or not view["name"] or view["id"] in view_ids:
                raise ValueError("Invalid or duplicate incident view")
            view_ids.add(view["id"])
            self.incident_views.append({key: view[key] for key in ("id", "name", "cel")})
        self.role_groups = {}
        for role, groups in document.get("roles", {}).items():
            if role not in {"admin", "responder", "viewer", "noc"}:
                raise ValueError(f"Unsupported role in team policy: {role}")
            if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
                raise ValueError(f"Invalid groups for role {role}")
            self.role_groups[role] = frozenset(groups)

        self.teams = {}
        self.zone_to_team = {}
        for item in document.get("teams", []):
            team_id = item["id"]
            if not isinstance(team_id, str) or not team_id or team_id in self.teams:
                raise ValueError(f"Invalid or duplicate team ID: {team_id}")
            groups = item.get("groups", [])
            zones = item.get("zones", [])
            visible_to = item.get("visible_to", [team_id])
            for name, values in (("groups", groups), ("zones", zones), ("visible_to", visible_to)):
                if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                    raise ValueError(f"Invalid {name} for team {team_id}")
            self.teams[team_id] = Team(
                id=team_id,
                groups=frozenset(groups),
                zones=frozenset(zones),
                visible_to=frozenset(visible_to),
            )
            for zone in zones:
                if zone in self.zone_to_team:
                    raise ValueError(f"Zone {zone} belongs to multiple teams")
                self.zone_to_team[zone] = team_id
        for team in self.teams.values():
            if not team.visible_to <= self.teams.keys():
                raise ValueError(f"Unknown visible_to team in {team.id}")

    def roles_for_groups(self, groups: set[str]) -> set[str]:
        return {role for role, mapped in self.role_groups.items() if groups & mapped}

    def teams_for_groups(self, groups: set[str]) -> frozenset[str]:
        return frozenset(team.id for team in self.teams.values() if groups & team.groups)

    def visible_teams(self, member_teams: set[str]) -> frozenset[str]:
        return frozenset(team.id for team in self.teams.values() if team.visible_to & member_teams)

    def team_for_zone(self, zone: str | None) -> str | None:
        if not isinstance(zone, str):
            return None
        return self.zone_to_team.get(zone)


def is_team_scoping_active() -> bool:
    return os.environ.get("AUTH_TYPE", "").lower() == "oauth2proxy"


@lru_cache(maxsize=1)
def _legacy_team_policy() -> TeamPolicy | None:
    path = os.environ.get("KEEP_TEAMS_CONFIG_FILE")
    inline = os.environ.get("KEEP_TEAMS_CONFIG")
    if not path and not inline:
        return None
    if path:
        with open(path, encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    else:
        document = yaml.safe_load(inline)
    if not isinstance(document, dict):
        raise ValueError("Team policy must be a YAML mapping")
    return TeamPolicy(document)


def get_team_policy(tenant_id=None) -> TeamPolicy | None:
    from keep.api.core.incident_configuration import active_configuration

    snapshot = active_configuration(tenant_id)
    return TeamPolicy(snapshot["documents"]["access"]) if snapshot else _legacy_team_policy()


get_team_policy.cache_clear = _legacy_team_policy.cache_clear
