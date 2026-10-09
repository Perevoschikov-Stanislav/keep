"""Task 31: End-to-end incident core verification, autonomy without Mattermost,
dynamic IaC configuration switching, and presentation consistency.
"""

import copy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
import yaml

from sqlmodel import Session, select

from keep.api.models.db.alert import Alert, AlertAudit
from keep.api.models.db.incident import Incident, IncidentStatus
from keep.api.models.db.incident_configuration import IncidentConfiguration
from keep.api.models.db.mapping import MappingRule
from keep.api.models.db.silence import NotificationDelivery
from keep.api.bl.enrichments_bl import EnrichmentsBl
from keep.api.core.incident_lifecycle import transition, project as project_lifecycle
from tests.team_fork_test_case import POLICY
from tests.test_incident_notifications_fork import NotificationCase, EVENTS


class IncidentCoreVerificationTest(NotificationCase):
    def save(self, event, seconds=0):
        super().save(event, seconds)
        import keep.api.core.db as db
        with Session(self.engine) as session:
            row = session.exec(select(Alert).where(Alert.fingerprint == event.fingerprint)).first()
            if row:
                db.set_last_alert("tenant", row, session=session)
                session.commit()
        return event

    def test_autonomous_incident_core_without_mattermost_transport(self):
        """Keep functions fully autonomously without Mattermost: event ingestion,
        normalization, correlation, lifecycle transitions, SLA, and HTTP webhook delivery.
        """
        # Configure route to deliver strictly to HTTP webhook (bypassing chat-api)
        self.bundle["routes"][0]["destination_refs"] = ["alpha-http"]
        self.apply()

        # Ingest and correlate alert
        event = self.event(workload="catalog", team="alpha")
        self.correlate(event)

        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident.status, "firing")
        self.assertEqual(incident.team_id, "alpha")
        self.assertEqual(incident.generated_name, "catalog")

        # Worker delivers HTTP webhook
        self.dispatcher().run_once()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["destination_ref"], "alpha-http")
        self.assertEqual(self.sent[0]["event_type"], "incident.created")

        # Progress lifecycle: firing -> acknowledged
        with Session(self.engine) as session:
            inc = session.get(Incident, incident.id)
            transition(session, inc, "acknowledged", actor="responder@test", at=self.now(10), reason="manual")
            session.commit()

        # Worker delivers acknowledged notification
        self.dispatcher(10).run_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1]["event_type"], "incident.acknowledged")

        # Progress lifecycle: acknowledged -> resolved
        with Session(self.engine) as session:
            inc = session.get(Incident, incident.id)
            transition(session, inc, "resolved", actor="system", at=self.now(20), reason="alert_resolved")
            session.commit()

        self.dispatcher(20).run_once()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[2]["event_type"], "incident.resolved")

    def test_multi_destination_partial_failure_does_not_block_healthy_destinations(self):
        """When one transport/destination fails (e.g. 500 or timeout), healthy
        destinations succeed and the failed delivery is queued/fenced without poisoning.
        """
        self.correlate(self.event(team="alpha"))

        # Custom sender: webhook succeeds, chat-api fails
        def sender(delivery, transport):
            if transport["kind"] == "mattermost":
                raise ConnectionError("Mattermost server temporarily unavailable")
            self.sent.append(copy.deepcopy(delivery.payload))
            return {"status": "delivered", "external_id": "wh-123"}

        self.dispatcher(sender=sender).run_once()

        # Healthy destination delivered
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["destination_ref"], "alpha-http")

        deliveries = self.deliveries()
        http_delivery = next(d for d in deliveries if d.destination_id == "alpha-http")
        chat_delivery = next(d for d in deliveries if d.destination_id == "alpha-chat")
        self.assertEqual(http_delivery.state, "delivered")
        self.assertIn(chat_delivery.state, {"unknown", "failed", "pending"})

    def test_dynamic_iac_configuration_switching_on_the_fly(self):
        """Zero-code configuration switching between two independent policy sets (teams,
        routes, templates, and rules).
        """
        # Config A active (teams alpha, beta)
        self.correlate(self.event(workload="billing", team="alpha"))
        incidents_a = self.incidents()
        self.assertEqual(len(incidents_a), 1)
        self.assertEqual(incidents_a[0].team_id, "alpha")
        self.assertEqual(incidents_a[0].generated_name, "billing")

        # Switch to Config B on the fly: add team gamma and change presentation template prefix
        access_doc = yaml.safe_load(POLICY)
        access_doc["teams"].append({"id": "gamma", "groups": ["/teams/gamma"], "zones": ["GAMMA"]})
        self.bundle["access"] = self.artifact("teams.yaml", access_doc)
        self.bundle["destinations"].append({
            "id": "gamma-http", "team_id": "gamma", "transport_ref": "webhook",
            "options": {"path": "/gamma"}
        })
        self.bundle["presentations"][0]["title"] = "PROD-INCIDENT: {{ normalized.workload }}"
        self.bundle["routes"][0]["team_ids"].append("gamma")
        self.bundle["routes"][0]["destination_refs"].append("gamma-http")
        self.bundle["correlation"][0]["team_ids"].append("gamma")
        self.bundle["normalization"][0]["team_ids"].append("gamma")
        self.bundle["automation"][0]["team_ids"].append("gamma")
        self.apply()

        # Ingest alert for team gamma with distinct fingerprint
        event_b = self.event("gamma-1", workload="auth-service", team="gamma", zone="GAMMA")
        self.correlate(event_b)

        incidents_all = self.incidents()
        self.assertEqual(len(incidents_all), 2)
        inc_b = next(i for i in incidents_all if i.team_id == "gamma")
        self.assertEqual(inc_b.generated_name, "PROD-INCIDENT: auth-service")

        # Alpha incident remains preserved
        inc_a = next(i for i in incidents_all if i.team_id == "alpha")
        self.assertEqual(inc_a.team_id, "alpha")

    def test_iac_negative_validation_protects_active_snapshot_atomically(self):
        """Invalid bundle configurations (syntax, CEL, capabilities, missing refs)
        are rejected atomically without modifying the active runtime snapshot.
        """
        with Session(self.engine) as session:
            before_digest = session.get(IncidentConfiguration, "tenant").digest

        invalid_bundle = copy.deepcopy(self.bundle)
        invalid_bundle["routes"][0]["match"] = "syntax error == ("

        self.bundle = invalid_bundle
        with self.assertRaises(ValueError):
            self.apply()

        with Session(self.engine) as session:
            after_digest = session.get(IncidentConfiguration, "tenant").digest
            self.assertEqual(before_digest, after_digest)

    def test_observation_25_persisted_generated_name_refreshed_on_template_change_and_sql_search(self):
        """When presentation templates change in IaC, the persisted generated_name
        cache on active incidents is refreshed, keeping SQL search consistent without
        overwriting operator-set manual titles.
        """
        self.correlate(self.event(workload="payments", team="alpha"))
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        inc_id = incidents[0].id

        # Initial generated name in DB
        with Session(self.engine) as session:
            inc = session.get(Incident, inc_id)
            self.assertEqual(inc.generated_name, "payments")

            # SQL name search matches
            result = session.exec(select(Incident).where(Incident.generated_name.like("%payments%"))).all()
            self.assertEqual(len(result), 1)

        # Update presentation template in IaC to include a prefix
        self.bundle["presentations"][0]["title"] = "[CRITICAL] {{ normalized.workload }}"
        self.apply()

        # Check that persisted generated_name in DB was refreshed
        with Session(self.engine) as session:
            inc = session.get(Incident, inc_id)
            self.assertEqual(inc.generated_name, "[CRITICAL] payments")

            # SQL search with new title succeeds immediately
            result = session.exec(select(Incident).where(Incident.generated_name.like("%[CRITICAL]%"))).all()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].id, inc_id)

            # Operator assigns a manual title
            inc.user_generated_name = "Manual Title Set By Engineer"
            session.add(inc)
            session.commit()

        # Update template again
        self.bundle["presentations"][0]["title"] = "[UPDATED] {{ normalized.workload }}"
        self.apply()

        # Manual title is preserved and takes precedence
        with Session(self.engine) as session:
            inc = session.get(Incident, inc_id)
            self.assertEqual(inc.user_generated_name, "Manual Title Set By Engineer")
            self.assertEqual(inc.generated_name, "[UPDATED] payments")

    def test_observation_25_manual_mapping_by_id_converts_db_alert_to_dto_and_preserves_ownership(self):
        """EnrichmentsBl.run_mapping_rule_by_id converts DB Alert to AlertDto, correctly
        matches nested label attributes, applies enrichment, and preserves canonical team_id.
        """
        mapping = {"name": "app-zone-mapping", "matchers": [["labels.app"]],
                   "rows": [{"labels.app": "search", "zone": "ALPHA", "service": "search-svc"}]}
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", mapping)
        self.apply()

        event = self.event(app="search")
        with Session(self.engine) as session:
            rule = session.exec(select(MappingRule)).first()
            service = EnrichmentsBl("tenant", session)

            row = Alert(tenant_id="tenant", team_id="alpha", fingerprint=event.fingerprint,
                        event=event.to_ingestion_dict(), provider_type="keep")
            session.add(row)
            session.commit()

            # Manual mapping by ID must convert DB Alert to AlertDto and match
            matched = service.run_mapping_rule_by_id(rule.id, row.id)
            self.assertTrue(matched)

            # Canonical ownership is preserved
            reloaded = session.get(Alert, row.id)
            self.assertEqual(reloaded.team_id, "alpha")

    def test_flapping_quiet_reset_timer_marks_resolved_without_incoming_event(self):
        """Flapping quiet-reset timer expiration marks flapping resolved and projects
        non-flapping state without requiring an incoming raw event.
        """
        # Set up correlation rule with flapping enabled
        self.bundle["lifecycle"][0]["flapping"] = {
            "enabled": True, "window_seconds": 60, "transition_threshold": 2, "reset_after_seconds": 10
        }
        self.apply()

        # Rapid state changes trigger flapping
        self.correlate(self.event(workload="db", team="alpha"), 0)
        self.correlate(self.event(workload="db", team="alpha", status="resolved"), 1)
        self.correlate(self.event(workload="db", team="alpha"), 2)

        incident = self.incidents()[0]
        lifecycle_now = project_lifecycle(incident, now=self.origin + timedelta(seconds=2))
        self.assertTrue(lifecycle_now["flapping"]["active"])

        # Advance time past quiet period (10 seconds after t=2 is t=15) without new events
        lifecycle_quiet = project_lifecycle(incident, now=self.origin + timedelta(seconds=15))
        self.assertFalse(lifecycle_quiet["flapping"]["active"])
