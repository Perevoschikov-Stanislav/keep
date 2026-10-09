"""Task 25: normalization is derived data, never operator or ownership state."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, select

from keep.api.models.alert import AlertDto
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.incident import Incident
from keep.api.models.incident import IncidentDto, IncidentDtoIn
from tests.test_incident_provisioning_fork import IncidentProvisioningCase


def field(name, *sources, extract=None, literal=None):
    result = {"name": name, "sources": list(sources), "missing": {"mode": "mark_unknown"}}
    if extract:
        result["extract"] = {"pattern": extract, "group": 1}
    if literal:
        result["missing"] = {"mode": "literal", "value": literal}
    return result


class EventNormalizationTest(IncidentProvisioningCase):
    def setUp(self):
        super().setUp()
        self.bundle["normalization"] = [{
            "id": "explicit", "team_ids": ["alpha", "beta"], "priority": 100,
            "match": "true", "presentation_ref": "object",
            "fields": [field("cluster", "labels.cluster"), field("namespace", "labels.namespace"),
                       field("kind", "labels.kind"), field("resource", "labels.resource", "labels.pod", "labels.node"),
                       field("workload", "labels.workload", "labels.owner"),
                       field("service", "labels.service", "labels.app"),
                       field("environment", "labels.environment", literal="unspecified")],
        }]
        self.bundle["presentations"] = [{"id": "object", "title": "{{ normalized.kind }}: {{ normalized.resource }}",
                                         "description": "{{ normalized.namespace }} / {{ normalized.cluster }}",
                                         "fields": [{"path": "normalized.service", "label": "Service", "order": 20},
                                                    {"path": "normalized.resource", "label": "Object", "order": 10}],
                                         "links": [{"label": "Runbook", "url_template": "https://runbooks.test/objects/{{ normalized.resource }}"}]}]
        self.apply()

    def normalize(self, *, team="alpha", **labels):
        from keep.api.core.event_normalization import normalize_event
        event = AlertDto(name="Original alert", status="firing", labels=labels, team_id=team)
        return normalize_event("tenant", event)

    def test_ordered_aliases_preserve_original_labels_and_status(self):
        event = self.normalize(workload="owner-first", owner="ignored", app="catalog", pod="p-1")
        self.assertEqual(event.normalized["workload"], "owner-first")
        self.assertEqual(event.normalized["service"], "catalog")
        self.assertEqual(event.labels["owner"], "ignored")
        self.assertEqual(event.name, "Original alert")
        self.assertEqual(event.status, "firing")
        self.assertEqual(event.normalization["fields"]["workload"]["source"], "labels.workload")

    def test_regex_is_fallback_with_priority_configured_in_iac(self):
        rule = self.bundle["normalization"][0]
        rule["match"] = "has(labels.workload) || has(labels.owner)"
        fallback = copy.deepcopy(rule)
        fallback.update(id="pod-fallback", priority=10, match="has(labels.pod)")
        fallback["fields"] = [field("resource", "labels.pod"), field("workload", "labels.pod", extract=r"^(.*)-[a-z0-9]{8,10}-[a-z0-9]{5}$")]
        self.bundle["normalization"].append(fallback)
        self.apply()
        for pod in ("catalog-5f4d889abc-abcde", "catalog-6e5a778def-vwxyz"):
            event = self.normalize(pod=pod)
            self.assertEqual(event.normalized["resource"], pod)
            self.assertEqual(event.normalized["workload"], "catalog")
            self.assertEqual(event.normalization["fields"]["workload"]["method"], "regex")
        self.assertEqual(self.normalize(pod="catalog-5f4d889abc-abcde", owner="explicit-owner").normalized["workload"], "explicit-owner")

    def test_fallback_and_bad_types_are_unknown_not_identity(self):
        from keep.api.core.event_normalization import known_normalized_field
        event = self.normalize(resource={"name": "bad"}, app=["bad"])
        self.assertIsNone(event.normalized["resource"])
        self.assertEqual(event.normalized["environment"], "unspecified")
        self.assertFalse(known_normalized_field(event.dict(), "normalized.environment"))
        self.assertFalse(known_normalized_field(event.dict(), "normalized.resource"))
        self.assertEqual(event.normalization["fields"]["resource"]["reason"], "invalid_type")

    def test_scope_and_incoming_claims_cannot_select_another_team(self):
        from keep.api.core.event_normalization import normalize_event
        self.bundle["normalization"][0]["team_ids"] = ["alpha"]
        self.apply()
        event = AlertDto(name="foreign", team_id="beta", labels={"team_id": "alpha", "service": "same"},
                         normalized={"service": "forged"}, normalization={"policy_id": "explicit"},
                         presentation={"title": "forged"})
        normalize_event("tenant", event)
        self.assertEqual(event.team_id, "beta")
        self.assertIsNone(event.normalized)
        self.assertIsNone(event.presentation)

    def test_presentation_order_escapes_url_and_marks_missing(self):
        event = self.normalize(kind="database", resource="catalog/a?b=<script>", service="catalog")
        self.assertEqual(event.presentation["title"], "database: catalog/a?b=<script>")
        self.assertEqual([item["label"] for item in event.presentation["fields"]], ["Object", "Service"])
        self.assertEqual(event.presentation["links"][0]["url"], "https://runbooks.test/objects/catalog%2Fa%3Fb%3D%3Cscript%3E")
        self.assertEqual(event.presentation["description"], "unknown / unknown")
        self.assertIn("normalized.cluster", event.presentation["missing_fields"])

    def test_quoted_kubernetes_alias_and_sensitive_alias_validation(self):
        source = self.bundle["normalization"][0]["fields"][5]
        source["sources"] = ['labels["app.kubernetes.io/name"]', "labels.service"]
        self.apply()
        event = self.normalize(**{"app.kubernetes.io/name": "identity-api", "service": "fallback"})
        self.assertEqual(event.normalized["service"], "identity-api")
        source["sources"] = ['labels["client_secret"]']
        with self.assertRaisesRegex(ValueError, "credential source"):
            self.candidate()

    def test_match_evaluation_error_does_not_match_or_drop_event(self):
        self.bundle["normalization"][0]["match"] = "labels.absent == 'x'"
        self.apply()
        event = self.normalize(service="retained")
        self.assertIsNone(event.normalized)
        self.assertEqual(event.labels["service"], "retained")

    def test_normalized_service_is_used_by_incident_aggregation_and_unknown_stays_unknown(self):
        from keep.api.utils.alert_utils import extract_service_from_alert
        event = self.normalize(app="configured-service", deployment="legacy-heuristic")
        self.assertEqual(extract_service_from_alert(event), "configured-service")
        event = self.normalize(deployment="legacy-heuristic")
        self.assertIsNone(extract_service_from_alert(event))

    def test_manual_mapping_by_id_remains_callable_and_projection_does_not_persist(self):
        from keep.api.bl.enrichments_bl import EnrichmentsBl
        from keep.api.models.db.mapping import MappingRule
        mapping = {"name": "derived-owner", "matchers": [["labels.namespace"]],
                   "rows": [{"labels.namespace": "monitoring", "zone": "ALPHA"}]}
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", mapping)
        self.apply()
        event = self.normalize(namespace="monitoring")
        with Session(self.engine) as session:
            rule = session.exec(select(MappingRule)).one()
            service = EnrichmentsBl("tenant", session)
            with patch.object(service, "enrich_entity") as write, patch.object(service, "_track_enrichment_event") as audit:
                service.run_mapping_rules(event, persist=False)
                self.assertEqual(event.zone, "ALPHA")
                write.assert_not_called()
                audit.assert_not_called()
                row = Alert(tenant_id="tenant", team_id="alpha", fingerprint=event.fingerprint, event=event.to_ingestion_dict(), provider_type="keep")
                session.add(row)
                session.commit()
                # Manual mapping by ID converts DB Alert to DTO, matches nested fields,
                # and applies enrichment while preserving canonical ownership.
                self.assertTrue(service.run_mapping_rule_by_id(rule.id, row.id))
                write.assert_called_once()

    def test_contract_fixtures_execute_with_both_iac_configurations(self):
        import yaml
        from keep.api.core.event_normalization import normalize_event
        root = Path(__file__).resolve().parents[1] / "docs/fork/incident-core"
        fixtures = json.loads((root / "fixtures-v1.json").read_text())["normalization_events"]
        for name in ("a", "b"):
            bundle = yaml.safe_load((root / "examples" / name / "bundle.yaml").read_text())
            snapshot = {"digest": name, "bundle": bundle}
            with patch("keep.api.core.event_normalization.active_configuration", return_value=snapshot):
                for fixture in fixtures:
                    with self.subTest(config=name, fixture=fixture["id"]):
                        event = AlertDto(name="fixture", team_id=bundle["normalization"][0]["team_ids"][0], **fixture["event"])
                        normalize_event("tenant", event)
                        self.assertEqual(event.normalization["policy_id"], fixture["normalization_ref"])
                        for key, expected in fixture["expected"][name].items():
                            self.assertEqual(event.normalized[key], expected)

    def test_runtime_examples_use_arbitrary_team_sets_and_valid_artifacts(self):
        from keep.api.bl.incident_provisioning import Candidate
        root = Path(__file__).resolve().parents[1] / "config/event-normalization.example"
        candidates = [Candidate.from_file(root / name / "bundle.yaml", "keep") for name in ("a", "b")]
        teams = [{item["id"] for item in candidate.resources if item["kind"] == "teams"} for candidate in candidates]
        self.assertTrue(teams[0].isdisjoint(teams[1]))
        for candidate in candidates:
            self.assertTrue({"normalization", "presentations"} <= {item["kind"] for item in candidate.resources})

    def test_metadata_and_templates_do_not_change_dedup_hash_payload(self):
        event = self.normalize(kind="node", node="worker-9")
        from keep.api.core.event_normalization import deduplication_payload
        before = deduplication_payload(event)
        event.normalization["config_digest"] = "changed"
        event.presentation["title"] = "changed"
        self.assertEqual(before, deduplication_payload(event))
        event.normalized["resource"] = "worker-10"
        self.assertNotEqual(before, deduplication_payload(event))

    def test_incident_collects_objects_descriptions_and_source_links(self):
        from keep.api.core.event_normalization import refresh_incident_presentation, project_incident
        definition = self.bundle["presentations"][0]
        definition["description"] = "{{ incident.collections.details }}"
        definition["fields"] = [{"path": "incident.collections.objects", "label": "Pods", "order": 0}]
        definition["collections"] = [
            {"id": "objects", "sources": ["normalized.resource"]},
            {"id": "details", "sources": ["annotations.description", "description"]},
        ]
        definition["source_links"] = [
            {"label": "Prometheus", "sources": ["generatorURL"]},
            {"label": "Runbook", "sources": ["annotations.runbook_url"]},
        ]
        self.apply()
        a = self.normalize(resource="pod-b", kind="workload", service="catalog")
        b = self.normalize(resource="pod-a", kind="workload", service="catalog")
        a.annotations = {"description": "Second pod fails", "runbook_url": "https://docs.test/failure"}
        b.annotations = {"description": "First pod fails", "runbook_url": "https://docs.test/failure"}
        a.generatorURL = 'https://prom.test/graph?expr=up{pod="pod-b"}'
        b.generatorURL = "javascript:alert(1)"
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id="alpha", user_generated_name="Operator title", user_summary="Operator note")
            session.add(incident)
            refresh_incident_presentation("tenant", incident, session, events=[a.dict(), b.dict(), a.dict()])
            session.commit()
            view = project_incident("tenant", incident)
            self.assertEqual(view["presentation"]["fields"][0]["value"], "pod-a\npod-b")
            self.assertTrue(view["presentation"]["fields"][0]["known"])
            self.assertIn("First pod fails", view["presentation"]["description"])
            self.assertIn("Second pod fails", view["presentation"]["description"])
            self.assertEqual(sum(link["label"] == "Runbook" for link in view["presentation"]["links"]), 1)
            self.assertFalse(any(link["url"].startswith("javascript:") for link in view["presentation"]["links"]))
            self.assertEqual((incident.user_generated_name, incident.user_summary), ("Operator title", "Operator note"))

    def test_collection_sources_activity_and_limits_are_configurable(self):
        from keep.api.core.event_normalization import refresh_incident_presentation, project_incident
        definition = self.bundle["presentations"][0]
        definition["fields"] = [{"path": "incident.collections.targets", "label": "Targets"}]
        definition["collections"] = [{"id": "targets", "sources": ["labels.resource"], "max_items": 1, "max_chars": 32}]
        self.apply()
        events = [self.normalize(resource=value, owner="other-" + value).dict() for value in ("pod-a", "pod-b", "pod-c")]
        events[-1]["status"] = "resolved"
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id="alpha")
            session.add(incident)
            refresh_incident_presentation("tenant", incident, session, events=events)
            self.assertEqual(project_incident("tenant", incident)["presentation"]["fields"][0]["value"], "pod-a\n… (+1 more)")
            definition["collections"][0]["sources"] = ["labels.owner"]
            definition["collections"][0]["max_items"] = 3
            definition["prefer_active_alerts"] = False
            self.apply()
            refresh_incident_presentation("tenant", incident, session, events=events)
            rendered = project_incident("tenant", incident)["presentation"]["fields"][0]["value"]
            self.assertTrue(rendered.startswith("other-pod-a"))
            self.assertLessEqual(len(rendered), 32)

    def test_collection_and_link_sources_reject_credentials_and_unknown_templates(self):
        from keep.api.core.incident_contract import ContractError
        for source in ("labels.access_token", "annotations.password", "team_id"):
            with self.subTest(source=source):
                self.bundle["presentations"][0]["collections"] = [{"id": "unsafe", "sources": [source]}]
                with self.assertRaises(ContractError):
                    self.candidate()
        self.bundle["presentations"][0]["collections"] = []
        self.bundle["presentations"][0]["fields"] = [{"path": "incident.collections.not_configured", "label": "Wrong"}]
        with self.assertRaises(ContractError):
            self.candidate()

    def test_source_links_drop_credential_urls_and_keep_configured_order(self):
        from keep.api.core.event_normalization import source_content
        definition = self.bundle["presentations"][0]
        definition["source_links"] = [{"label": "Runbook", "sources": ["annotations.runbook_url", "generatorURL"], "max_items": 1}]
        events = [{"annotations": {"runbook_url": "https://user:password@docs.test"}, "generatorURL": "https://prom.test/graph"},
                  {"annotations": {"runbook_url": "https://docs.test?access_token=hidden"}},
                  {"generatorURL": "https://prom.test/" + "x" * 2100}]
        content = source_content({"bundle": self.bundle}, events)["object"]
        self.assertEqual(content["links"], [{"label": "Runbook", "url": "https://prom.test/graph"}])

    def test_missing_normalized_identity_uses_provider_fingerprint_and_skips_grouping(self):
        from keep.providers.base.base_provider import BaseProvider
        from keep.rulesengine.rulesengine import RulesEngine
        from keep.api.models.db.rule import Rule
        event = self.normalize()
        original = event.fingerprint
        self.assertEqual(BaseProvider.get_alert_fingerprint(event, ["normalized.resource"]), original)
        self.assertEqual(RulesEngine("tenant")._calc_rule_fingerprint(event, Rule(grouping_criteria=["normalized.environment"])), [])

    def test_normalized_custom_fingerprints_have_unambiguous_field_and_team_boundaries(self):
        from keep.providers.base.base_provider import BaseProvider
        a = self.normalize(kind="ab", resource="c")
        b = self.normalize(kind="a", resource="bc")
        other = self.normalize(team="beta", kind="ab", resource="c")
        fields = ["normalized.kind", "normalized.resource"]
        fingerprints = {BaseProvider.get_alert_fingerprint(event, fields) for event in (a, b, other)}
        self.assertEqual(len(fingerprints), 3)

    def test_dedup_ignore_for_absent_normalized_fields_keeps_event(self):
        from types import SimpleNamespace
        from keep.api.alert_deduplicator.alert_deduplicator import AlertDeduplicator
        event = self.normalize(team="unassigned")
        self.assertIsNone(event.normalized)
        result = AlertDeduplicator("tenant")._apply_deduplication_rule(
            event, SimpleNamespace(id="fixture", ignore_fields=["normalized.resource", "labels.not_present"]), {"other": "hash"})
        self.assertIs(result, event)
        self.assertTrue(result.alert_hash)

    def test_preview_marks_new_events_and_does_not_renormalize_saved_alert(self):
        event = self.normalize(resource="old", service="catalog")
        fingerprint = self.save_alert(event)
        self.bundle["normalization"][0]["fields"][3]["sources"] = ["labels.different"]
        preview = self.service.preview(self.candidate())
        self.assertEqual(preview["normalization_impact"]["scope"], "new_events")
        self.assertFalse(preview["normalization_impact"]["rewrite_history"])
        self.apply()
        with Session(self.engine) as session:
            stored = session.exec(select(Alert).where(Alert.fingerprint == fingerprint)).one()
            self.assertEqual(stored.event["normalized"]["resource"], "old")
        self.assertIsNone(self.normalize(resource="old").normalized["resource"])

    def test_ingestion_never_publishes_an_event_without_its_mapped_owner(self):
        import keep.api.tasks.process_event_task as task
        from keep.rulesengine.rulesengine import RulesEngine

        mapping = {"name": "ownership", "matchers": [["labels.namespace"]],
                   "rows": [{"labels.namespace": "north", "zone": "ALPHA"}]}
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", mapping)
        commit = Session.commit
        snapshots = []

        def observe_commit(session):
            commit(session)
            # An independent reader must see ownership on the first durable row.
            with self.engine.connect() as connection:
                owners = connection.execute(select(Alert.team_id).where(
                    Alert.tenant_id == "tenant",
                )).scalars().all()
            if owners:
                snapshots.append(owners)

        for normalization in (True, False):
            with self.subTest(normalization=normalization):
                if not normalization:
                    owned = next(item for item in self.service.status()["resources"]
                                 if item["kind"] == "normalization" and item["id"] == "explicit")
                    self.bundle["normalization"] = []
                    self.bundle["deletions"] = [{"kind": "normalization", "id": "explicit",
                                                 "expected_resource_digest": owned["digest"]}]
                self.apply()
                snapshots.clear()
                raw = {"name": "Ownership visibility", "status": "firing", "labels": {
                    "namespace": "north", "resource": str(normalization)}, "team_id": "forged"}
                with patch.object(Session, "commit", observe_commit), \
                     patch.object(task.WorkflowManager, "get_instance"), \
                     patch.object(RulesEngine, "send_workflow_event"), \
                     patch.object(task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False):
                    task.process_event({}, "tenant", "keep", None, None, None, "fixture",
                                       raw, notify_client=False)
                self.assertTrue(snapshots, "No committed alert was observed")
                self.assertTrue(all(owner == "alpha" for owners in snapshots for owner in owners),
                                f"Published owner changed during ingestion: {snapshots}")

    def test_full_ingestion_correlates_normalized_fields_and_preserves_ownership_raw_and_manual_state(self):
        import keep.api.core.db as db
        import keep.api.tasks.process_event_task as task
        from keep.api.models.db.alert import AlertRaw
        from keep.providers.base.base_provider import BaseProvider
        from keep.rulesengine.rulesengine import RulesEngine
        mapping = {"name": "ownership", "matchers": [["labels.namespace"]],
                   "rows": [{"labels.namespace": "north", "zone": "ALPHA"}, {"labels.namespace": "south", "zone": "BETA"}]}
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", mapping)
        rule = {"ruleName": "Service incidents", "sqlQuery": {"sql": "(1=1)", "params": {}},
                "celQuery": "status == 'firing'", "timeframeInSeconds": 600, "timeUnit": "seconds",
                "groupingCriteria": ["normalized.kind", "normalized.service"], "threshold": 1,
                "incidentNameTemplate": "Automatic {{ alert.normalized.service }}"}
        self.bundle["rules"] = [{"id": "services", "artifact": self.artifact("rule.yaml", rule)}]
        self.apply()
        db.create_deduplication_rule("tenant", "normalized", "test", None, "keep", "test",
                                     fingerprint_fields=["normalized.kind", "normalized.service", "normalized.resource"])

        def ingest(pod, namespace):
            raw = {"name": "Replica issue", "status": "firing", "labels": {
                "pod": pod, "namespace": namespace, "service": "catalog", "kind": "workload", "cluster": "lab"},
                "team_id": "forged", "normalized": {"service": "forged"}}
            task.process_event({}, "tenant", "keep", None, None, None, "fixture", copy.deepcopy(raw), notify_client=False)
            return raw

        with patch.object(task.WorkflowManager, "get_instance"), patch.object(RulesEngine, "send_workflow_event") as published, \
             patch.object(task, "KEEP_STORE_RAW_ALERTS", False), patch.object(task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False):
            raw = ingest("catalog-a", "north")
            ingest("catalog-a", "south")
            with Session(self.engine) as session:
                incidents = session.exec(select(Incident)).all()
                self.assertEqual({incident.team_id for incident in incidents}, {"alpha", "beta"})
                alpha = next(incident for incident in incidents if incident.team_id == "alpha")
                alpha.user_generated_name, alpha.user_summary, alpha.assignee = "Operator", "Manual notes", "responder"
                identifier = alpha.id
                session.add(alpha)
                session.commit()
            ingest("catalog-renamed", "north")
            with Session(self.engine) as session:
                alerts = session.exec(select(Alert)).all()
                self.assertEqual(len(alerts), 3)
                self.assertEqual(len({alert.fingerprint for alert in alerts}), 3)
                self.assertEqual({alert.event["normalized"]["service"] for alert in alerts}, {"catalog"})
                self.assertEqual({alert.event["status"] for alert in alerts}, {"firing"})
                self.assertEqual(len(session.exec(select(Incident)).all()), 2)
                alpha = session.get(Incident, identifier)
                self.assertEqual(alpha.alerts_count, 2)
                dto = IncidentDto.from_db_incident(alpha, session=session, with_silences=False)
                self.assertEqual((dto.name, dto.user_summary, dto.assignee), ("Operator", "Manual notes", "responder"))
                self.assertEqual(dto.normalized["service"], "catalog")
                self.assertEqual(dto.services, ["catalog"])
                raws = session.exec(select(AlertRaw).where(AlertRaw.error == False)).all()
                self.assertEqual(len(raws), 3)
                self.assertIn(raw, [row.raw_alert for row in raws])
            self.assertEqual(published.call_count, 3)

    def test_unsupported_actions_and_broken_references_retain_active_config(self):
        before = self.service.status()["active_digest"]
        self.bundle["presentations"][0]["actions"] = [{"command": "delete", "label": "Delete"}]
        with self.assertRaisesRegex(ValueError, "command"):
            self.candidate()
        self.bundle["presentations"][0].pop("actions")
        self.bundle["normalization"][0]["presentation_ref"] = "absent"
        with self.assertRaises(ValueError):
            self.candidate()
        self.assertEqual(before, self.service.status()["active_digest"])

    def save_alert(self, event):
        with Session(self.engine) as session:
            row = Alert(tenant_id="tenant", team_id=event.team_id, fingerprint=event.fingerprint,
                        event=event.to_ingestion_dict(), provider_type="keep")
            session.add(row)
            session.flush()
            session.commit()
            import keep.api.core.db as db
            db.set_last_alert("tenant", row, session=session)
        return event.fingerprint

    def test_incident_manual_values_survive_event_and_template_updates(self):
        import keep.api.core.db as db
        from keep.api.core.event_normalization import refresh_incident_presentation
        event = self.normalize(kind="database", resource="primary", namespace="storage", cluster="lab")
        fingerprint = self.save_alert(event)
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id="alpha", user_generated_name="Operator title", user_summary="Operator notes", assignee="engineer")
            session.add(incident)
            session.commit()
            db.add_alerts_to_incident("tenant", incident, [fingerprint], session=session)
            dto = IncidentDto.from_db_incident(incident, session=session, with_silences=False)
            self.assertEqual(dto.generated_name, "database: primary")
            self.assertEqual(dto.name, "Operator title")
            self.assertEqual(dto.user_summary, "Operator notes")
            self.assertEqual(dto.assignee, "engineer")
            self.bundle["presentations"][0]["title"] = "Object {{ normalized.resource }}"
            self.apply()
            dto = IncidentDto.from_db_incident(incident, session=session, with_silences=False)
            self.assertEqual(dto.generated_name, "Object primary")
            self.assertEqual(dto.user_summary, "Operator notes")
            self.assertEqual(dto.name, "Operator title")
            self.assertEqual(incident.normalization_context["normalized"]["resource"], "primary")

    def test_partial_update_keeps_notes_and_owner_change_clears_foreign_presentation(self):
        import keep.api.core.db as db
        fingerprint = self.save_alert(self.normalize(kind="node", node="worker-1"))
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id="alpha", user_summary="Manual notes")
            session.add(incident)
            session.commit()
            identifier = incident.id
            db.add_alerts_to_incident("tenant", incident, [fingerprint], session=session)
        row = db.update_incident_from_dto_by_id("tenant", identifier, IncidentDtoIn(assignee="engineer"))
        self.assertEqual(row.user_summary, "Manual notes")
        row = db.update_incident_from_dto_by_id("tenant", identifier, IncidentDtoIn(team_id="beta"))
        self.assertIsNone(row.normalization_context)
        self.assertIsNone(row.generated_name)
        self.assertEqual(row.user_summary, "Manual notes")

    def test_operator_api_cannot_write_normalization_context_or_generated_name(self):
        import keep.api.core.db as db
        dto = IncidentDtoIn(team_id="alpha", user_generated_name="Operator", generated_name="forged",
                            normalization_context={"normalized": {"service": "secret"}})
        row = db.create_incident_from_dto("tenant", dto)
        self.assertIsNone(row.generated_name)
        self.assertIsNone(row.normalization_context)
        db.update_incident_from_dto_by_id("tenant", row.id, dto)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Incident, row.id).generated_name)
            self.assertIsNone(session.get(Incident, row.id).normalization_context)

    def test_ambiguous_objects_remain_unknown_and_unlink_rebuilds_context(self):
        import keep.api.core.db as db
        a = self.normalize(kind="pvc", resource="data-a", service="storage")
        b = self.normalize(kind="pvc", resource="data-b", service="storage")
        b.fingerprint = "other-pvc"
        fingerprints = [self.save_alert(event) for event in (a, b)]
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id="alpha")
            session.add(incident)
            session.commit()
            identifier = incident.id
            db.add_alerts_to_incident("tenant", incident, fingerprints, session=session)
            dto = IncidentDto.from_db_incident(incident, with_silences=False)
            self.assertIsNone(dto.normalized["resource"])
            self.assertEqual(dto.normalization["fields"]["resource"]["reason"], "multiple_values")
        db.remove_alerts_to_incident_by_incident_id("tenant", identifier, [b.fingerprint])
        with Session(self.engine) as session:
            dto = IncidentDto.from_db_incident(session.get(Incident, identifier), with_silences=False)
            self.assertEqual(dto.normalized["resource"], "data-a")
