import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from keep.identitymanager.rbac import Admin, Responder, Viewer
from keep.identitymanager.team_access import visible_team_ids
from keep.identitymanager.team_policy import TeamPolicy, get_team_policy
from keep.identitymanager.authenticatedentity import AuthenticatedEntity


POLICY = """
version: 1
roles:
  admin: [/roles/admin]
  responder: [/roles/responder]
  viewer: [/roles/viewer]
teams:
  - id: alpha
    groups: [/teams/alpha]
    zones: [A]
    visible_to: [alpha]
  - id: beta
    groups: [/teams/beta]
    zones: [B]
    visible_to: [beta]
"""


class TeamPolicyTest(unittest.TestCase):
    def tearDown(self):
        get_team_policy.cache_clear()

    def test_roles_do_not_grant_team_membership(self):
        with patch.dict(os.environ, {"KEEP_TEAMS_CONFIG": POLICY}):
            get_team_policy.cache_clear()
            policy = get_team_policy()
            groups = {"/roles/responder", "/teams/alpha"}
            self.assertEqual(policy.roles_for_groups(groups), {"responder"})
            self.assertEqual(policy.teams_for_groups(groups), frozenset({"alpha"}))
            self.assertEqual(policy.visible_teams({"alpha"}), frozenset({"alpha"}))
            self.assertEqual(policy.team_for_zone("B"), "beta")
            self.assertIsNone(policy.team_for_zone("unknown"))

    def test_lab_policy_hides_foreign_teams_and_separates_role_groups(self):
        lab = Path(__file__).resolve().parents[1] / "lab"
        policy = TeamPolicy(yaml.safe_load((lab / "team-policy.yaml").read_text()))
        memberships = yaml.safe_load((lab / "keycloak-memberships.yaml").read_text())
        self.assertEqual(policy.visibility, "team")
        role_groups = set().union(*policy.role_groups.values())
        team_groups = set().union(*(team.groups for team in policy.teams.values()))
        self.assertFalse(role_groups & team_groups)
        for groups in memberships["users"].values():
            roles = policy.roles_for_groups(set(groups))
            self.assertEqual(len(roles), 1)
            if "admin" not in roles:
                teams = policy.teams_for_groups(set(groups))
                self.assertTrue(teams)
                self.assertEqual(policy.visible_teams(set(teams)), teams)

    def test_visibility_can_be_shared_by_configuration(self):
        document = {
            "version": 1,
            "teams": [
                {"id": "alpha", "groups": ["/teams/alpha"], "zones": ["A"]},
                {
                    "id": "beta",
                    "groups": ["/teams/beta"],
                    "zones": ["B"],
                    "visible_to": ["alpha", "beta"],
                },
            ],
        }
        policy = TeamPolicy(document)
        self.assertEqual(policy.visible_teams({"alpha"}), frozenset({"alpha", "beta"}))
        with self.assertRaisesRegex(ValueError, "multiple teams"):
            TeamPolicy({"version": 1, "teams": [
                {"id": "alpha", "zones": ["A"]},
                {"id": "beta", "zones": ["A"]},
            ]})

    def test_role_scopes_separate_response_from_deletion(self):
        self.assertTrue(Responder.has_scopes(["update:incident", "update:alert"]))
        self.assertFalse(Responder.has_scopes(["delete:incident"]))
        self.assertFalse(Responder.has_scopes(["write:incident"]))
        self.assertTrue(Viewer.has_scopes(["read:incident"]))
        self.assertFalse(Viewer.has_scopes(["update:incident"]))
        self.assertTrue(Admin.has_scopes(["delete:incident"]))

    def test_visibility_is_deny_by_default_when_policy_is_enabled(self):
        with patch.dict(os.environ, {"AUTH_TYPE": "OAUTH2PROXY", "KEEP_TEAMS_CONFIG": POLICY}):
            get_team_policy.cache_clear()
            viewer = AuthenticatedEntity(tenant_id="tenant", email="viewer", role="viewer")
            self.assertEqual(visible_team_ids(viewer), frozenset())
            member = AuthenticatedEntity(
                tenant_id="tenant", email="member", role="responder",
                visible_teams=frozenset({"alpha"}),
            )
            self.assertEqual(visible_team_ids(member), frozenset({"alpha"}))
            admin = AuthenticatedEntity(tenant_id="tenant", email="admin", role="admin")
            self.assertIsNone(visible_team_ids(admin))


if __name__ == "__main__":
    unittest.main()
