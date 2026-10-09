"""Isolated policy and database fixtures for the fork's unittest suite."""

import os
import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from keep.identitymanager.team_policy import get_team_policy


POLICY = """
version: 1
roles:
  admin: [/roles/admin]
  responder: [/roles/responder]
  viewer: [/roles/viewer]
teams:
  - id: alpha
    groups: [/teams/alpha]
    zones: [ALPHA]
  - id: beta
    groups: [/teams/beta]
    zones: [BETA]
"""


class TeamPolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "AUTH_TYPE": "OAUTH2PROXY", "KEEP_TEAMS_CONFIG": POLICY,
        }))
        os.environ.pop("KEEP_TEAMS_CONFIG_FILE", None)
        get_team_policy.cache_clear()
        self.addCleanup(get_team_policy.cache_clear)


class TeamDatabaseTestCase(TeamPolicyTestCase):
    def setUp(self):
        super().setUp()
        import keep.api.core.alerts as alerts
        import keep.api.core.db as db
        import keep.api.core.facets as facets
        import keep.api.core.incidents as incidents
        import keep.identitymanager.team_backfill as team_backfill

        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        for module in (db, alerts, facets, incidents, team_backfill):
            self.enterContext(patch.object(module, "engine", self.engine))
        SQLModel.metadata.create_all(self.engine)
