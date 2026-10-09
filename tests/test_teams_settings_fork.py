"""Configured teams must remain scoped in the Settings API."""

import os
from unittest.mock import patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory
from keep.identitymanager.identity_managers.oauth2proxy import oauth2proxy_authverifier
from keep.identitymanager.team_policy import get_team_policy
from tests.team_fork_test_case import TeamPolicyTestCase


class TeamsSettingsTest(TeamPolicyTestCase):
    def setUp(self):
        super().setUp()
        from keep.api.routes.auth import teams

        self.routes = teams
        self.enterContext(patch.dict(os.environ, {
            "KEEP_OAUTH2_PROXY_USER_HEADER": "x-forwarded-email",
            "KEEP_OAUTH2_PROXY_ROLE_HEADER": "x-forwarded-groups",
        }))
        self.enterContext(patch.object(oauth2proxy_authverifier, "user_exists", return_value=False))
        self.enterContext(patch.object(oauth2proxy_authverifier, "create_user"))
        self.reset_client()

    def reset_client(self):
        get_team_policy.cache_clear()
        dependency = self.routes.get_teams.__defaults__[0].dependency
        app = FastAPI()
        app.include_router(self.routes.router, prefix="/auth/teams")
        app.dependency_overrides[dependency] = IdentityManagerFactory.get_auth_verifier(dependency.scopes)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def get(self, *groups):
        return self.client.get("/auth/teams", headers={
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": ", ".join(groups),
        })

    def test_admin_sees_full_configuration(self):
        response = self.get("/roles/admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "enabled": True, "visibility": "team",
            "teams": [
                {"id": "alpha", "groups": ["/teams/alpha"], "zones": ["ALPHA"], "visible_to": ["alpha"]},
                {"id": "beta", "groups": ["/teams/beta"], "zones": ["BETA"], "visible_to": ["beta"]},
            ],
        })

    def test_team_roles_cannot_discover_foreign_configuration(self):
        for role in ("responder", "viewer"):
            with self.subTest(role=role):
                response = self.get(f"/roles/{role}", "/teams/alpha")
                self.assertEqual(response.status_code, 200)
                self.assertEqual([team["id"] for team in response.json()["teams"]], ["alpha"])
                self.assertNotIn("beta", response.text)
                self.assertNotIn("BETA", response.text)

    def test_role_without_team_gets_no_teams(self):
        response = self.get("/roles/viewer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["teams"], [])

    def test_multiple_memberships_are_combined(self):
        response = self.get("/roles/viewer", "/teams/alpha", "/teams/beta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([team["id"] for team in response.json()["teams"]], ["alpha", "beta"])

    def test_shared_reading_does_not_disclose_inaccessible_audiences(self):
        document = yaml.safe_load(os.environ["KEEP_TEAMS_CONFIG"])
        document["teams"][1]["visible_to"] = ["alpha", "beta", "private-team"]
        document["teams"].append({
            "id": "private-team", "groups": ["/teams/private"], "zones": ["PRIVATE"],
        })
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(document)
        self.reset_client()
        response = self.get("/roles/viewer", "/teams/alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([team["id"] for team in response.json()["teams"]], ["alpha", "beta"])
        self.assertEqual(response.json()["teams"][1]["visible_to"], ["alpha", "beta"])
        self.assertNotIn("private", response.text)
        self.assertNotIn("PRIVATE", response.text)

    def test_shared_visibility_lists_all_teams(self):
        document = yaml.safe_load(os.environ["KEEP_TEAMS_CONFIG"])
        document["visibility"] = "all"
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(document)
        self.reset_client()
        response = self.get("/roles/viewer", "/teams/alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["visibility"], "all")
        self.assertEqual([team["id"] for team in response.json()["teams"]], ["alpha", "beta"])

    def test_unconfigured_or_inactive_policy_is_distinct_from_no_membership(self):
        entity = AuthenticatedEntity(tenant_id="tenant", email="admin", role="admin")
        with patch.dict(os.environ, {"KEEP_TEAMS_CONFIG": ""}):
            get_team_policy.cache_clear()
            response = self.routes.get_teams(entity)
            self.assertFalse(response.enabled)
            self.assertIsNone(response.visibility)
            self.assertEqual(response.teams, [])
        with patch.dict(os.environ, {"AUTH_TYPE": "NOAUTH"}):
            get_team_policy.cache_clear()
            self.assertFalse(self.routes.get_teams(entity).enabled)

    def test_empty_configured_policy_is_enabled(self):
        document = yaml.safe_load(os.environ["KEEP_TEAMS_CONFIG"])
        document["teams"] = []
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(document)
        self.reset_client()
        response = self.get("/roles/admin")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.assertEqual(response.json()["teams"], [])

    def test_endpoint_requires_identity_and_a_mapped_role(self):
        self.assertEqual(self.client.get("/auth/teams").status_code, 401)
        self.assertEqual(self.get("/teams/alpha").status_code, 403)
        self.assertEqual(self.get("/unknown-role", "/teams/alpha").status_code, 403)
        self.assertEqual(self.routes.get_teams.__defaults__[0].dependency.scopes, ["read:settings"])

    def test_configuration_cannot_be_changed_through_api(self):
        for method in ("post", "put", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/auth/teams")
                self.assertEqual(response.status_code, 405)
