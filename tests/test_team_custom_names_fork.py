"""Exercise arbitrary IaC team IDs against ingestion and database access checks."""

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

import yaml
from fastapi import HTTPException
from sqlmodel import Session, select
from starlette.requests import Request

import keep.api.tasks.process_event_task as process_event_task
from keep.api.core.alerts import query_last_alerts
from keep.api.core.db import get_last_alerts
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.incidents import get_last_incidents_by_cel
from keep.api.models.alert import AlertDto
from keep.api.models.alert import BatchEnrichAlertRequestBody
from keep.api.models.db.alert import Alert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.tenant import Tenant
from keep.api.models.query import QueryDto
from keep.api.routes.incidents import get_incident
from keep.api.routes.incidents import assign_incident, get_incident_views
from keep.api.routes.auth.users import get_my_permissions
import keep.api.routes.alerts as alert_routes
import keep.api.routes.providers as provider_routes
from keep.identitymanager.identity_managers.oauth2proxy import oauth2proxy_authverifier
from keep.identitymanager.team_access import require_alert_access, visible_team_ids, writable_team_ids
from keep.identitymanager.team_policy import get_team_policy
from tests.team_fork_test_case import TeamDatabaseTestCase


class CustomTeamNamesTest(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.team_ids = ("team-1", f"team-{uuid4().hex}")
        # Membership groups and zones deliberately differ from the team IDs.
        self.groups = ("/org/support", "/org/engineering")
        self.zones = ("rack-7", "segment-42")
        self.document = {
            "version": 1,
            "roles": {
                "admin": ["/roles/admin"],
                "responder": ["/roles/responder"],
                "viewer": ["/roles/viewer"],
            },
            "teams": [
                {"id": team, "groups": [group], "zones": [zone]}
                for team, group, zone in zip(self.team_ids, self.groups, self.zones)
            ],
        }
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(self.document)
        os.environ["KEEP_OAUTH2_PROXY_USER_HEADER"] = "x-forwarded-email"
        os.environ["KEEP_OAUTH2_PROXY_ROLE_HEADER"] = "x-forwarded-groups"
        get_team_policy.cache_clear()
        with Session(self.engine) as session:
            session.add(Tenant(id=SINGLE_TENANT_UUID, name="Custom team test"))
            session.commit()

    def authenticate(self, *groups):
        verifier = oauth2proxy_authverifier.Oauth2proxyAuthVerifier(["read:alert"])
        request = Request({
            "type": "http",
            "headers": [
                (b"x-forwarded-email", b"member@example.test"),
                (b"x-forwarded-groups", ", ".join(groups).encode()),
            ],
        })
        # User provisioning is separate from the policy and database access tested here.
        with patch.object(oauth2proxy_authverifier, "user_exists", return_value=False), \
             patch.object(oauth2proxy_authverifier, "create_user"):
            return verifier.authenticate(request, "", None, None)

    def ingest_and_link_incidents(self):
        events = [
            AlertDto(
                id=None, name=f"custom-{index}", fingerprint=f"custom-{index}",
                status="firing", severity="warning", source=["prometheus"],
                lastReceived=datetime.utcnow().isoformat(), zone=zone,
                # The sender's claimed owner must not override the zone policy.
                team_id=self.team_ids[1],
            )
            for index, zone in enumerate((*self.zones, "unknown-zone"))
        ]
        for index, event in enumerate(events):
            event.alert_hash = f"custom-hash-{index}"
        with patch.object(process_event_task, "EnrichmentsBl") as enrichments, \
             patch.object(process_event_task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False), \
             patch.object(process_event_task, "KEEP_AUDIT_EVENTS_ENABLED", False), \
             patch.object(process_event_task, "get_enrichment_with_session", return_value=None):
            enrichments.return_value.run_extraction_rules.side_effect = lambda alert: alert
            with Session(self.engine) as session:
                saved = getattr(process_event_task, "__save_to_db")(
                    SINGLE_TENANT_UUID, "prometheus", session, [], events, [], "custom-provider"
                )
                self.assertEqual(
                    [alert.team_id for alert in saved], [*self.team_ids, None]
                )

        self.incident_ids = {}
        self.fingerprints = {}
        with Session(self.engine) as session:
            for index, team in enumerate((*self.team_ids, None)):
                alert = session.exec(
                    select(Alert).where(Alert.fingerprint == f"custom-{index}")
                ).one()
                self.assertEqual(alert.team_id, team)
                incident = Incident(
                    tenant_id=SINGLE_TENANT_UUID, team_id=team,
                    user_summary="Custom incident", generated_summary="",
                )
                session.add(incident)
                session.flush()
                session.add(LastAlertToIncident(
                    tenant_id=SINGLE_TENANT_UUID, fingerprint=alert.fingerprint,
                    incident_id=incident.id,
                ))
                self.incident_ids[team] = incident.id
                self.fingerprints[team] = alert.fingerprint
            session.commit()

    def assert_visible_data(self, entity, expected_teams):
        allowed = visible_team_ids(entity)
        expected_fingerprints = {self.fingerprints[team] for team in expected_teams}
        alerts = get_last_alerts(entity.tenant_id, allowed_team_ids=allowed)
        self.assertEqual({alert.fingerprint for alert in alerts}, expected_fingerprints)
        alerts, count = query_last_alerts(
            entity.tenant_id, QueryDto(cel=""), allowed_team_ids=allowed
        )
        self.assertEqual(count, len(expected_teams))
        self.assertEqual({alert.fingerprint for alert in alerts}, expected_fingerprints)
        incidents, count = get_last_incidents_by_cel(
            entity.tenant_id, allowed_team_ids=allowed
        )
        self.assertEqual(count, len(expected_teams))
        self.assertEqual(
            {incident.id for incident in incidents},
            {self.incident_ids[team] for team in expected_teams},
        )
        for team in (*self.team_ids, None):
            with self.subTest(team=team, role=entity.role):
                if team in expected_teams:
                    require_alert_access(entity, self.fingerprints[team])
                    incident = get_incident(self.incident_ids[team], authenticated_entity=entity)
                    self.assertEqual(incident.team_id, team)
                else:
                    for call in (
                        lambda: require_alert_access(entity, self.fingerprints[team]),
                        lambda: get_incident(self.incident_ids[team], authenticated_entity=entity),
                    ):
                        with self.assertRaises(HTTPException) as error:
                            call()
                        self.assertEqual(error.exception.status_code, 404)

    def test_inline_policy_isolates_team_1_and_a_random_team(self):
        self.ingest_and_link_incidents()
        for team, group in zip(self.team_ids, self.groups):
            entity = self.authenticate("/roles/responder", group)
            self.assertEqual(entity.role, "responder")
            self.assertEqual(entity.teams, frozenset({team}))
            self.assert_visible_data(entity, {team})

        no_membership = self.authenticate("/roles/responder", "/org/unmapped")
        self.assert_visible_data(no_membership, set())
        with self.assertRaises(HTTPException) as error:
            self.authenticate(self.groups[0])
        self.assertEqual(error.exception.status_code, 403)

    def test_file_policy_combines_memberships_and_keeps_admin_unrestricted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as policy_file:
            yaml.safe_dump(self.document, policy_file)
            policy_file.flush()
            os.environ["KEEP_TEAMS_CONFIG_FILE"] = policy_file.name
            # Conflicting inline config proves the mounted file has priority.
            os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump({
                "version": 1, "roles": self.document["roles"], "teams": [],
            })
            get_team_policy.cache_clear()
            self.ingest_and_link_incidents()
            member = self.authenticate("/roles/viewer", *self.groups)
            self.assertEqual(member.role, "viewer")
            self.assertEqual(member.teams, frozenset(self.team_ids))
            self.assert_visible_data(member, set(self.team_ids))
            admin = self.authenticate("/roles/admin")
            self.assertEqual(admin.teams, frozenset())
            self.assert_visible_data(admin, {*self.team_ids, None})

    def test_shared_visibility_does_not_allow_writing_other_teams(self):
        self.document["visibility"] = "all"
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(self.document)
        get_team_policy.cache_clear()
        self.ingest_and_link_incidents()
        member = self.authenticate("/roles/responder", self.groups[0])
        self.assert_visible_data(member, {*self.team_ids, None})
        self.assertEqual(writable_team_ids(member), frozenset({"team-1"}))
        self.assertEqual(get_my_permissions(member)["writable_teams"], ["team-1"])

        for team in (self.team_ids[1], None):
            with self.assertRaises(HTTPException) as error:
                require_alert_access(member, self.fingerprints[team], for_write=True)
            self.assertEqual(error.exception.status_code, 403)
            with Session(self.engine) as session:
                with self.assertRaises(HTTPException) as error:
                    assign_incident(self.incident_ids[team], member, session)
                self.assertEqual(error.exception.status_code, 403)
                self.assertIsNone(session.get(Incident, self.incident_ids[team]).assignee)

        with Session(self.engine) as session:
            assign_incident(self.incident_ids["team-1"], member, session)
            self.assertEqual(session.get(Incident, self.incident_ids["team-1"]).assignee, member.email)

        viewer = self.authenticate("/roles/viewer", self.groups[0])
        self.assert_visible_data(viewer, {*self.team_ids, None})
        with self.assertRaises(HTTPException) as error:
            require_alert_access(viewer, self.fingerprints["team-1"], for_write=True)
        self.assertEqual(error.exception.status_code, 403)

        with patch.object(alert_routes, "EnrichmentsBl") as enrichments:
            with self.assertRaises(HTTPException) as error:
                alert_routes.batch_enrich_alerts(
                    BatchEnrichAlertRequestBody(
                        fingerprints=[self.fingerprints[team] for team in self.team_ids],
                        enrichments={"note": "Must not be written"},
                    ),
                    authenticated_entity=member, dispose_on_new_alert=False, session=None,
                )
            self.assertEqual(error.exception.status_code, 403)
            enrichments.assert_not_called()
        with self.assertRaises(HTTPException) as error:
            provider_routes.get_provider_logs("provider", authenticated_entity=member)
        self.assertEqual(error.exception.status_code, 403)
        with Session(self.engine) as session:
            session.add(LastAlertToIncident(
                tenant_id=member.tenant_id,
                fingerprint=self.fingerprints[self.team_ids[1]],
                incident_id=self.incident_ids["team-1"],
            ))
            session.commit()
            self.assertEqual(get_incident(self.incident_ids["team-1"], member).team_id, "team-1")
            with self.assertRaises(HTTPException) as error:
                assign_incident(self.incident_ids["team-1"], member, session)
            self.assertEqual(error.exception.status_code, 403)
            self.assertIn("Read only", error.exception.detail)

    def test_configured_incident_views_filter_by_team_and_linked_alerts(self):
        self.document["visibility"] = "all"
        self.document["incident_views"] = [
            {"id": "arbitrary-view", "name": "TEAM 1", "cel": "team_id == 'team-1'"},
            {"id": "scanner", "name": "SCANNER", "cel": "alert.namespace.startsWith('scanner') || alert.name.startsWith('Scanner')"},
        ]
        os.environ["KEEP_TEAMS_CONFIG"] = yaml.safe_dump(self.document)
        get_team_policy.cache_clear()
        self.ingest_and_link_incidents()
        with Session(self.engine) as session:
            alert = session.exec(select(Alert).where(Alert.fingerprint == self.fingerprints[None])).one()
            alert.event = {**alert.event, "namespace": "scanner-local"}
            session.commit()

        member = self.authenticate("/roles/viewer", self.groups[0])
        views = get_incident_views(member)
        self.assertEqual([view["name"] for view in views], ["ALL", "TEAM 1", "SCANNER"])
        for view, expected in zip(views, ({*self.team_ids, None}, {"team-1"}, {None})):
            with self.subTest(view=view["id"]):
                items, count = get_last_incidents_by_cel(
                    member.tenant_id, cel=view["cel"], allowed_team_ids=visible_team_ids(member)
                )
                self.assertEqual(count, len(expected))
                self.assertEqual({item.id for item in items}, {self.incident_ids[team] for team in expected})


if __name__ == "__main__":
    unittest.main()
