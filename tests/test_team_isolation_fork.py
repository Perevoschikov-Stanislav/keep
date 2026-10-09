"""Run in the Keep API image: python tests/test_team_isolation_fork.py."""

import asyncio
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, select
from starlette.requests import Request

import keep.api.core.facets as facets_core
import keep.api.routes.alerts as alert_routes  # Register the models with SQLModel.
import keep.api.routes.ai as ai_routes
import keep.api.routes.dashboard as dashboard_routes
import keep.api.routes.facets as facet_routes
import keep.api.routes.incidents as incident_routes
import keep.api.routes.mapping as mapping_routes
import keep.api.routes.extraction as extraction_routes
import keep.api.routes.workflows as workflow_routes
import keep.api.routes.providers as provider_routes
import keep.api.routes.settings as settings_routes
import keep.api.tasks.process_event_task as process_event_task
from keep.api.consts import fingerprints_for_poll_payload
from keep.api.core.db import (
    get_incident_alerts_by_incident_id,
    get_incident_for_grouping_rule,
    get_last_alerts,
    get_linked_providers,
    merge_incidents_to_id,
)
from keep.api.core.alerts import query_last_alerts
from keep.api.core.incidents import get_last_incidents_by_cel
from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
from keep.api.models.db.enrichment_event import EnrichmentEvent, EnrichmentLog
from keep.api.models.db.facet import Facet
from keep.api.models.alert import AlertDto
from keep.api.models.db.incident import Incident
from keep.api.models.db.tenant import Tenant
from keep.api.models.facet import CreateFacetDto, UpdateFacetDto
from keep.api.models.incident import IncidentDto
from keep.api.models.query import QueryDto
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from keep.identitymanager.identity_managers.oauth2proxy.oauth2proxy_authverifier import Oauth2proxyAuthVerifier
from keep.identitymanager.rbac import get_role_by_role_name
from keep.identitymanager.team_access import (
    accessible_alert_fingerprints,
    require_alert_access,
    require_incident_access,
)
from tests.team_fork_test_case import TeamDatabaseTestCase, TeamPolicyTestCase


class TeamIsolationTest(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        with Session(self.engine) as session:
            session.add(Tenant(id="tenant", name="Tenant"))
            self.incidents = {}
            self.alert_ids = {}
            for team in ("alpha", "beta"):
                fingerprint = f"{team}-alert"
                alert = Alert(
                    tenant_id="tenant", team_id=team, provider_type="prometheus",
                    provider_id=f"{team}-provider",
                    event={"name": fingerprint}, fingerprint=fingerprint,
                )
                incident = Incident(
                    tenant_id="tenant", team_id=team,
                    user_summary=f"{team} summary", generated_summary="",
                )
                session.add_all((alert, incident))
                session.flush()
                session.add(LastAlert(
                    tenant_id="tenant", fingerprint=fingerprint,
                    alert_id=alert.id, timestamp=now, first_timestamp=now,
                ))
                session.add(LastAlertToIncident(
                    tenant_id="tenant", fingerprint=fingerprint,
                    incident_id=incident.id,
                ))
                self.incidents[team] = incident.id
                self.alert_ids[team] = alert.id
            session.commit()

        self.alpha = AuthenticatedEntity(
            tenant_id="tenant", email="alpha@example.test", role="responder",
            teams=frozenset({"alpha"}),
            visible_teams=frozenset({"alpha"}),
        )

    def test_custom_facet_edit_persists_and_keeps_tenant_boundary(self):
        facet = facets_core.create_facet(
            "tenant", "alert", CreateFacetDto(name="Cluster", property_path="cluster")
        )
        updated = facets_core.update_facet(
            "tenant", "alert", facet.id,
            UpdateFacetDto(name="Namespace", property_path="namespace", description="Updated"),
        )
        self.assertEqual((updated.name, updated.property_path), ("Namespace", "namespace"))
        self.assertIsNone(facets_core.update_facet(
            "another-tenant", "alert", facet.id, UpdateFacetDto(name="Wrong tenant")
        ))
        with Session(self.engine) as session:
            saved = session.get(Facet, UUID(facet.id))
            self.assertEqual((saved.name, saved.property_path, saved.description),
                             ("Namespace", "namespace", "Updated"))

    def test_workflow_body_and_replay_enforce_team_access(self):
        with patch.object(workflow_routes, "WorkflowStore") as store, patch.object(
            workflow_routes, "WorkflowManager"
        ) as manager:
            store.return_value.get_workflow.return_value = SimpleNamespace(
                workflow_permissions=None, workflow_revision=1,
            )
            scheduler = manager.get_instance.return_value.scheduler
            scheduler.handle_manual_event_workflow.return_value = "execution-id"
            for team in ("alpha", "beta"):
                with Session(self.engine) as session:
                    incident = json.loads(IncidentDto.from_db_incident(
                        session.get(Incident, self.incidents[team])
                    ).json())
                requests = (
                    {"body": {"fingerprint": f"{team}-alert"}},
                    {"body": {"body": {"fingerprint": f"{team}-alert"}}},
                    {"body": {"type": "incident", "body": incident}},
                    {"event_type": "alert", "event_id": str(self.alert_ids[team])},
                )
                for kwargs in requests:
                    with self.subTest(team=team, request=kwargs):
                        scheduler.handle_manual_event_workflow.reset_mock()
                        params = dict(event_type=None, event_id=None, body=None)
                        params.update(kwargs)
                        if team == "beta":
                            with self.assertRaises(HTTPException) as error:
                                workflow_routes.run_workflow(
                                    "workflow", authenticated_entity=self.alpha, **params
                                )
                            self.assertEqual(error.exception.status_code, 404)
                            scheduler.handle_manual_event_workflow.assert_not_called()
                        else:
                            response = workflow_routes.run_workflow(
                                "workflow", authenticated_entity=self.alpha, **params
                            )
                            self.assertEqual(response["workflow_execution_id"], "execution-id")
                            scheduler.handle_manual_event_workflow.assert_called_once()
                            scheduled = scheduler.handle_manual_event_workflow.call_args.args[4]
                            if isinstance(scheduled, AlertDto):
                                self.assertEqual(scheduled.fingerprint, "alpha-alert")
                            else:
                                self.assertEqual(scheduled.id, self.incidents["alpha"])

            admin = AuthenticatedEntity(tenant_id="tenant", email="admin", role="admin")
            response = workflow_routes.run_workflow(
                "workflow", event_type=None, event_id=None,
                body={"fingerprint": "beta-alert"}, authenticated_entity=admin,
            )
            self.assertEqual(response["status"], "success")

    def test_enrichment_history_lists_counts_and_details_are_team_scoped(self):
        event_ids = {}
        with Session(self.engine) as session:
            for kind in ("mapping", "extraction"):
                for team in ("alpha", "beta"):
                    event = EnrichmentEvent(
                        tenant_id="tenant", status="success", enrichment_type=kind,
                        rule_id=1, alert_id=self.alert_ids[team],
                        enriched_fields={"team": team},
                    )
                    session.add(event)
                    session.flush()
                    event_ids[kind, team] = event.id
                    session.add(EnrichmentLog(
                        tenant_id="tenant", enrichment_event_id=event.id, message=team,
                    ))
            session.commit()
        viewer = AuthenticatedEntity(
            tenant_id="tenant", email="viewer", role="viewer",
            visible_teams=frozenset({"alpha"}),
        )
        admin = AuthenticatedEntity(tenant_id="tenant", email="admin", role="admin")
        no_teams = AuthenticatedEntity(tenant_id="tenant", email="viewer", role="viewer")
        for kind, routes in (("mapping", mapping_routes), ("extraction", extraction_routes)):
            for entity, expected_count in ((viewer, 1), (admin, 2), (no_teams, 0)):
                with self.subTest(kind=kind, role=entity.role, count=expected_count):
                    page = routes.get_enrichment_events(
                        1, limit=20, offset=0, authenticated_entity=entity,
                    )
                    self.assertEqual(page.count, expected_count)
                    self.assertEqual(len(page.items), expected_count)
                    if entity is viewer:
                        self.assertEqual(page.items[0].enriched_fields, {"team": "alpha"})
                        empty_page = routes.get_enrichment_events(
                            1, limit=1, offset=1, authenticated_entity=entity,
                        )
                        self.assertEqual((empty_page.count, empty_page.items), (1, []))
            own = routes.get_enrichment_event_logs(
                1, event_ids[kind, "alpha"], authenticated_entity=viewer,
            )
            self.assertEqual(own.enrichment_event.enriched_fields, {"team": "alpha"})
            for event_id in (event_ids[kind, "beta"], uuid4()):
                with self.assertRaises(HTTPException) as error:
                    routes.get_enrichment_event_logs(
                        1, event_id, authenticated_entity=viewer,
                    )
                self.assertEqual(error.exception.status_code, 404)
        with Session(self.engine) as session:
            session.add(Alert(
                tenant_id="tenant", team_id="beta", provider_type="prometheus",
                event={"name": "old"}, fingerprint="alpha-alert",
            ))
            session.commit()
        for kind, routes in (("mapping", mapping_routes), ("extraction", extraction_routes)):
            page = routes.get_enrichment_events(1, limit=20, offset=0, authenticated_entity=viewer)
            self.assertEqual((page.count, page.items), (0, []))
            with self.assertRaises(HTTPException) as error:
                routes.get_enrichment_event_logs(
                    1, event_ids[kind, "alpha"], authenticated_entity=viewer,
                )
            self.assertEqual(error.exception.status_code, 404)

    def test_team_users_see_custom_facets(self):
        for entity_type, fetch in (
            ("alert", alert_routes.fetch_alert_facets),
            ("incident", incident_routes.fetch_inicident_facets),
        ):
            facets_core.create_facet(
                tenant_id="tenant", entity_type=entity_type,
                facet=CreateFacetDto(name="Cluster", property_path="cluster"),
            )
            names = [facet.name for facet in fetch(authenticated_entity=self.alpha)]
            self.assertIn("Cluster", names, entity_type)

    def test_cross_team_merge_is_rejected_before_mutation(self):
        with self.assertRaises(HTTPException) as error:
            merge_incidents_to_id(
                "tenant", [self.incidents["alpha"]], self.incidents["beta"]
            )
        self.assertEqual(error.exception.status_code, 409)
        alerts, count = get_incident_alerts_by_incident_id(
            "tenant", self.incidents["alpha"]
        )
        self.assertEqual(count, 1)
        self.assertEqual(alerts[0].fingerprint, "alpha-alert")

    def test_list_filters_before_returning_results(self):
        alerts = get_last_alerts("tenant", allowed_team_ids=frozenset({"alpha"}))
        self.assertEqual([alert.fingerprint for alert in alerts], ["alpha-alert"])
        queried, alert_count = query_last_alerts(
            "tenant", QueryDto(cel=""), allowed_team_ids=frozenset({"alpha"})
        )
        self.assertEqual(alert_count, 1)
        self.assertEqual([alert.fingerprint for alert in queried], ["alpha-alert"])
        incidents, count = get_last_incidents_by_cel(
            "tenant", allowed_team_ids=frozenset({"alpha"}),
        )
        self.assertEqual(count, 1)
        self.assertEqual([incident.id for incident in incidents], [self.incidents["alpha"]])

    def test_direct_links_and_search_fingerprints_are_filtered(self):
        self.assertEqual(fingerprints_for_poll_payload(["beta-alert"]), [])
        require_alert_access(self.alpha, "alpha-alert")
        require_incident_access(self.alpha, self.incidents["alpha"])
        self.assertEqual(
            accessible_alert_fingerprints(
                self.alpha, {"alpha-alert", "beta-alert"}
            ),
            {"alpha-alert"},
        )
        for call in (
            lambda: require_alert_access(self.alpha, "beta-alert"),
            lambda: require_incident_access(self.alpha, self.incidents["beta"]),
        ):
            with self.assertRaises(HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code, 404)

    def test_mixed_fingerprint_history_is_hidden(self):
        with Session(self.engine) as session:
            session.add(Alert(
                tenant_id="tenant", team_id="beta", provider_type="prometheus",
                event={"name": "old event"}, fingerprint="alpha-alert",
            ))
            session.commit()
        with self.assertRaises(HTTPException):
            require_alert_access(self.alpha, "alpha-alert")
        with self.assertRaises(HTTPException):
            require_incident_access(self.alpha, self.incidents["alpha"])
        self.assertEqual(
            get_last_alerts("tenant", allowed_team_ids=frozenset({"alpha"})),
            [],
        )
        queried, alert_count = query_last_alerts(
            "tenant", QueryDto(cel=""), allowed_team_ids=frozenset({"alpha"})
        )
        self.assertEqual((queried, alert_count), ([], 0))
        incidents, incident_count = get_last_incidents_by_cel(
            "tenant", allowed_team_ids=frozenset({"alpha"})
        )
        self.assertEqual((incidents, incident_count), ([], 0))

    def test_linked_provider_activity_is_scoped_to_team(self):
        linked = get_linked_providers("tenant", frozenset({"alpha"}))
        self.assertEqual(
            [(provider_type, provider_id) for provider_type, provider_id, _ in linked],
            [("prometheus", "alpha-provider")],
        )

    def test_global_metrics_and_provider_export_are_closed_to_team_users(self):
        for call in (
            lambda: dashboard_routes.get_metric_widgets(
                authenticated_entity=self.alpha
            ),
            lambda: provider_routes.get_installed_providers(
                authenticated_entity=self.alpha
            ),
            lambda: provider_routes.get_webhook_settings(
                provider_type="prometheus", authenticated_entity=self.alpha
            ),
            lambda: settings_routes.webhook_settings(authenticated_entity=self.alpha),
            lambda: settings_routes.get_keys(authenticated_entity=self.alpha),
            lambda: asyncio.run(settings_routes.get_smtp_settings(
                authenticated_entity=self.alpha
            )),
        ):
            with self.assertRaises(HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code, 403)

    def test_paid_ai_stats_endpoint_is_closed_in_oss_mode(self):
        with patch.object(ai_routes, "OSS_ONLY", True):
            with self.assertRaises(HTTPException) as error:
                ai_routes.get_stats(authenticated_entity=self.alpha)
        self.assertEqual(error.exception.status_code, 404)

    def test_z_grouping_rule_reuses_incidents_within_each_team(self):
        rule = SimpleNamespace(id=uuid4(), timeframe=3600)
        created_ids = []
        with Session(self.engine) as session:
            for team in ("alpha", "beta"):
                incident = Incident(
                    tenant_id="tenant", team_id=team, rule_id=rule.id,
                    rule_fingerprint="shared-key", user_summary="", generated_summary="",
                )
                session.add(incident)
                session.flush()
                created_ids.append(incident.id)
            session.commit()
        try:
            for team, expected_id in zip(("alpha", "beta"), created_ids):
                incident, expired = get_incident_for_grouping_rule(
                    "tenant", rule, "shared-key", team_id=team
                )
                self.assertEqual(incident.id, expected_id)
                self.assertFalse(expired)
        finally:
            with Session(self.engine) as session:
                for incident_id in created_ids:
                    session.delete(session.get(Incident, incident_id))
                session.commit()

    def test_zz_event_owner_comes_from_current_mapping(self):
        event = AlertDto(
            id=None, name="mapped alert", status="firing", severity="warning",
            lastReceived=datetime.utcnow().isoformat(),
            source=["prometheus"], fingerprint="mapped-alert",
        )
        event.alert_hash = "mapped-hash"
        with patch.object(process_event_task, "EnrichmentsBl") as enrichments_bl, patch.object(
            process_event_task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False
        ), patch.object(
            process_event_task, "KEEP_AUDIT_EVENTS_ENABLED", False
        ), patch.object(
            process_event_task, "get_enrichment_with_session",
            return_value=SimpleNamespace(enrichments={"zone": "BETA"}),
        ):
            enrichments_bl.return_value.run_extraction_rules.side_effect = lambda alert: alert
            enrichments_bl.return_value.run_mapping_rules.side_effect = (
                lambda alert: setattr(alert, "zone", "ALPHA")
            )
            with Session(self.engine) as session:
                saved = getattr(process_event_task, "__save_to_db")(
                    "tenant", "prometheus", session, [], [event], [], "mapped-provider"
                )
                alert = session.exec(
                    select(Alert).where(Alert.fingerprint == "mapped-alert")
                ).one()
                self.assertEqual(alert.team_id, "alpha")
                self.assertEqual(alert.event["zone"], "ALPHA")
                self.assertEqual(saved[0].team_id, "alpha")
                self.assertEqual(saved[0].zone, "ALPHA")


class OAuthGroupMappingTest(TeamPolicyTestCase):
    def authenticate(self, groups):
        verifier = Oauth2proxyAuthVerifier(["read:alert"])
        request = Request({
            "type": "http",
            "headers": [
                (b"x-forwarded-email", b"user@example.test"),
                (b"x-forwarded-groups", groups.encode()),
            ],
        })
        with patch(
            "keep.identitymanager.identity_managers.oauth2proxy.oauth2proxy_authverifier.user_exists",
            return_value=False,
        ), patch(
            "keep.identitymanager.identity_managers.oauth2proxy.oauth2proxy_authverifier.create_user"
        ):
            return verifier.authenticate(request, "", None, None)

    def test_role_and_team_membership_are_independent(self):
        entity = self.authenticate("/roles/responder, /teams/alpha")
        self.assertEqual(entity.role, "responder")
        self.assertEqual(entity.teams, frozenset({"alpha"}))
        self.assertEqual(entity.visible_teams, frozenset({"alpha"}))
        admin = self.authenticate("/roles/admin, /roles/responder, /teams/alpha")
        self.assertEqual(admin.role, "admin")

    def test_unknown_groups_do_not_become_roles(self):
        with self.assertRaises(HTTPException) as error:
            self.authenticate("/some-unrelated-group, /teams/alpha")
        self.assertEqual(error.exception.status_code, 403)


class RouteScopeTest(unittest.TestCase):
    def scopes(self, router, method, path):
        route = next(
            route for route in router.routes
            if route.path == path and method in route.methods
        )
        return next(
            dependency.call.scopes
            for dependency in route.dependant.dependencies
            if hasattr(dependency.call, "scopes")
        )

    def test_response_and_destructive_routes_have_distinct_scopes(self):
        self.assertEqual(
            self.scopes(incident_routes.router, "POST", "/{incident_id}/status"),
            ["update:incident"],
        )
        for method, path in (
            ("DELETE", "/{incident_id}"),
            ("DELETE", "/bulk"),
            ("POST", "/merge"),
            ("POST", "/{incident_id}/split"),
        ):
            self.assertEqual(
                self.scopes(incident_routes.router, method, path),
                ["delete:incident"],
            )
        self.assertEqual(
            self.scopes(alert_routes.router, "DELETE", ""),
            ["delete:alert"],
        )
        self.assertEqual(
            self.scopes(alert_routes.router, "POST", "/{fingerprint}/assign/{last_received}"),
            ["read:alert"],
        )

    def test_only_admin_changes_facets_and_deletes_alerts(self):
        for method, path, scope in (
            ("POST", "", "write:facets"),
            ("PUT", "/{facet_id}", "write:facets"),
            ("DELETE", "/{facet_id}", "delete:facets"),
        ):
            self.assertEqual(self.scopes(facet_routes.router, method, path), [scope])
        for scope in ("write:facets", "delete:facets", "delete:alert"):
            self.assertTrue(get_role_by_role_name("admin").has_scopes([scope]))
            for role in ("responder", "viewer", "noc", "webhook"):
                self.assertFalse(
                    get_role_by_role_name(role).has_scopes([scope]), (role, scope)
                )

    def test_viewer_cannot_assign_alert(self):
        viewer = AuthenticatedEntity(
            tenant_id="tenant", email="viewer@example.test", role="viewer"
        )
        with self.assertRaises(HTTPException) as error:
            alert_routes.assign_alert(
                fingerprint="alpha-alert", last_received="now",
                authenticated_entity=viewer,
            )
        self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
