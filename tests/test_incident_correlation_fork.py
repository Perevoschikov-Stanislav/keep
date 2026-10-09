"""Task 26: explicit grouping, scope, missing data and reproducible episodes."""

import copy
from datetime import datetime, timedelta
from uuid import UUID
from unittest.mock import patch

from sqlmodel import Session, select

from keep.api.models.alert import AlertDto
from keep.api.models.db.alert import Alert, LastAlert
from keep.api.models.db.incident import Incident
from keep.api.models.db.rule import Rule
from keep.api.models.db.tenant import Tenant
from keep.api.models.incident import IncidentDto
from keep.rulesengine.rulesengine import RulesEngine
from tests.test_incident_provisioning_fork import IncidentProvisioningCase
from tests.test_event_normalization_fork import field


class CorrelationCase(IncidentProvisioningCase):
    def setUp(self):
        # Import before metadata.create_all, including the grouping lock table.
        import keep.api.core.incident_correlation  # noqa: F401
        super().setUp()
        with Session(self.engine) as session:
            session.add(Tenant(id="tenant", name="Correlation fixture"))
            session.commit()
        self.bundle["normalization"] = [{"id": "labels", "team_ids": ["alpha", "beta"], "priority": 1,
            "match": "true", "fields": [field(name, "labels." + name) for name in
                                      ("cluster", "namespace", "kind", "resource", "workload", "service")]}]
        self.bundle["presentations"] = [{"id": "workload", "title": "{{ normalized.workload }}",
            "description": "{{ normalized.namespace }} / {{ normalized.cluster }}",
            "fields": [{"path": "normalized.workload", "label": "Deployment", "order": 1}]}]
        self.bundle["lifecycle"] = [{"id": "basic", "resolve_on": "all_resolved",
            "reopen": {"mode": "new_incident", "within_seconds": 0, "ack": "reset", "assignee": "clear"},
            "flapping": {"enabled": False, "window_seconds": 60, "transition_threshold": 2, "reset_after_seconds": 60},
            "clock": "receive_time", "late_event_policy": "history_only"}]
        self.bundle["correlation"] = [{"id": "workload", "team_ids": ["alpha", "beta"], "priority": 100,
            "match": "normalized.kind == 'workload'", "group_by": ["normalized.cluster", "normalized.namespace", "normalized.workload"],
            "required_fields": ["normalized.cluster", "normalized.namespace", "normalized.workload"],
            "missing_required": "skip_correlation", "window_seconds": 60,
            "threshold": 1, "lifecycle_ref": "basic", "presentation_ref": "workload"}]
        self.apply()
        self.published = self.enterContext(patch.object(RulesEngine, "send_workflow_event"))
        self.origin = datetime(2026, 10, 5, 12, 0, 0)

    def event(self, fingerprint="p-1", *, team="alpha", status="firing", **labels):
        from keep.api.core.event_normalization import normalize_event
        values = {"cluster": "lab", "namespace": "ns", "kind": "workload", "workload": "catalog", "resource": fingerprint}
        values.update(labels)
        return normalize_event("tenant", AlertDto(name="Replica failure", fingerprint=fingerprint, team_id=team, status=status, labels=values))

    def save(self, event, seconds=0):
        import keep.api.core.db as db
        with Session(self.engine) as session:
            row = Alert(tenant_id="tenant", team_id=event.team_id, fingerprint=event.fingerprint,
                event=event.to_ingestion_dict(), provider_type="keep", timestamp=self.origin + timedelta(seconds=seconds))
            session.add(row)
            session.commit()
            event.id = str(row.id)
            event.event_id = str(row.id)
            db.set_last_alert("tenant", row, session=session)
        return event

    def correlate(self, event, seconds=0, *, save=True):
        if save:
            self.save(event, seconds)
        with Session(self.engine) as session:
            return RulesEngine("tenant").run_rules([event], session)

    def incidents(self):
        with Session(self.engine) as session:
            return session.exec(select(Incident).order_by(Incident.creation_time, Incident.id)).all()


class IncidentCorrelationTest(CorrelationCase):
    def test_workload_replicas_and_other_teams_and_workloads(self):
        for event in [self.event("p-1"), self.event("p-2"), self.event("beta-p", team="beta"), self.event("other-p", workload="other")]:
            self.correlate(event)
        rows = self.incidents()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row.alerts_count for row in rows), [1, 1, 2])
        self.assertEqual({row.team_id for row in rows}, {"alpha", "beta"})

    def test_missing_null_empty_stay_saved_and_have_distinct_diagnostics(self):
        for index, value in enumerate((None, "", {})):
            event = self.event(str(index), workload=value)
            self.assertEqual(self.correlate(event), [])
            self.assertEqual(event.correlation["decisions"][0]["reason"], "missing_required")
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Alert)).all()), 3)
        self.assertEqual(self.incidents(), [])

    def test_explicit_separate_alert_fallback_is_per_fingerprint(self):
        self.bundle["correlation"][0]["missing_required"] = "separate_alert"
        self.apply()
        self.correlate(self.event("incomplete-a", workload=None))
        self.correlate(self.event("incomplete-b", workload=None))
        self.correlate(self.event("incomplete-a", workload=None), 1)
        self.assertEqual(len(self.incidents()), 2)
        self.assertTrue(all(row.alerts_count == 1 for row in self.incidents()))

    def test_window_is_half_open_and_duplicate_delivery_does_not_move_it(self):
        first = self.event()
        self.correlate(first)
        self.correlate(first, save=False)
        self.correlate(self.event("p-2"), 59)
        self.assertEqual(len(self.incidents()), 1)
        self.correlate(self.event("p-3"), 60)
        self.assertEqual(len(self.incidents()), 2)
        self.assertEqual(self.published.call_count, 3)

    def test_threshold_counts_distinct_alerts_not_repeated_versions(self):
        self.bundle["correlation"][0]["threshold"] = 2
        self.apply()
        self.correlate(self.event())
        self.correlate(self.event(), 1)
        self.assertFalse(self.incidents()[0].is_visible)
        self.assertEqual(self.published.call_count, 0)
        self.correlate(self.event("p-2"), 2)
        self.assertTrue(self.incidents()[0].is_visible)
        self.assertEqual(self.published.call_args.args[-1], "created")

    def test_legacy_nested_missing_never_crashes_or_uses_shared_none(self):
        for criteria in (["not_present.deep.value"], ["labels.workload"]):
            event = AlertDto(name="legacy", labels={"workload": None})
            self.assertEqual(RulesEngine("tenant")._calc_rule_fingerprint(event, Rule(grouping_criteria=criteria)), [])

    def test_legacy_factory_return_survives_owned_session_closure(self):
        event = self.save(self.event())
        with Session(self.engine) as session:
            rule = Rule(tenant_id="tenant", name="Legacy", created_by="fixture", creation_time=datetime.utcnow(),
                        definition={"sql": "1=1", "params": {}}, definition_cel="true", timeframe=600,
                        grouping_criteria=["normalized.workload"])
            session.add(rule)
            session.commit()
            session.refresh(rule)
        engine = RulesEngine("tenant")
        key = engine._calc_rule_fingerprint(event, rule)[0][0]
        incident, created = engine._get_or_create_incident(rule, key, None, event)
        self.assertTrue(created)
        self.assertTrue(incident.id)
        self.assertEqual(incident.team_id, "alpha")

    def test_typed_keys_do_not_collapse_commas_or_value_types(self):
        from keep.api.core.incident_correlation import grouping_keys
        rule = self.bundle["correlation"][0]
        rule.update(group_by=["labels.a", "labels.b"], required_fields=["labels.a", "labels.b"])
        pairs = [("a,b", "c"), ("a", "b,c"), (1, "x"), ("1", "x"), (True, "x"), (["a", "b"], "c")]
        keys = [grouping_keys("tenant", self.event(a=a, b=b), rule, "revision")[0][0]["key"] for a, b in pairs]
        self.assertEqual(len(set(keys)), len(pairs))
        from keep.api.core.incident_correlation import MISSING, typed
        self.assertEqual([typed(value)[0] for value in (MISSING, None, "", 0, False)], ["missing", "null", "string", "int", "bool"])
        self.assertEqual(typed({"b": 2, "a": 1}), typed({"a": 1, "b": 2}))
        self.assertNotEqual(grouping_keys("other-tenant", self.event(a="a", b="b,c"), rule, "revision")[0][0]["key"], keys[1])

    def test_runtime_examples_accept_different_team_sets_and_parameters(self):
        from pathlib import Path
        from keep.api.bl.incident_provisioning import Candidate
        root = Path(__file__).resolve().parents[1] / "config/incident-correlation.example"
        candidates = [Candidate.from_file(root / name / "bundle.yaml", "keep") for name in ("a", "b")]
        teams = [{item["id"] for item in candidate.resources if item["kind"] == "teams"} for candidate in candidates]
        self.assertTrue(teams[0].isdisjoint(teams[1]))
        self.assertEqual([item.bundle["correlation_overlap"] for item in candidates], ["first_match", "parallel"])
        self.assertNotEqual(candidates[0].bundle["correlation"][0]["window_seconds"], candidates[1].bundle["correlation"][0]["window_seconds"])

    def test_missing_required_states_do_not_share_an_identity(self):
        self.bundle["correlation"][0].update(group_by=["labels.identity.nested"], required_fields=["labels.identity.nested"])
        self.apply()
        for index, (value, expected) in enumerate(((None, "missing"), ({"nested": None}, "null"), ({"nested": ""}, "empty"))):
            event = self.event(str(index), identity=value)
            self.correlate(event)
            self.assertEqual(event.correlation["decisions"][0]["missing_fields"][0]["state"], expected)
        self.assertEqual(self.incidents(), [])

    def second_rule(self):
        other = copy.deepcopy(self.bundle["correlation"][0])
        other.update(id="service", priority=10, group_by=["normalized.service"], required_fields=["normalized.service"])
        self.bundle["correlation"].append(other)
        return other

    def test_overlap_first_match_and_parallel_are_explicit(self):
        self.second_rule()
        self.apply()
        event = self.event(service="same")
        self.correlate(event)
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(event.correlation["decisions"][1]["reason"], "overlap_lower_priority")
        self.bundle["correlation_overlap"] = "parallel"
        self.apply()
        self.correlate(self.event("p-2", service="same"))
        self.assertEqual(len(self.incidents()), 3)
        self.assertEqual({row.correlation_context["rule_id"] for row in self.incidents()}, {"workload", "service"})

    def test_priority_changes_winner_without_rebinding_old_alert(self):
        other = self.second_rule()
        self.apply()
        original = self.event(service="same")
        self.correlate(original)
        old = self.incidents()[0]
        evidence = copy.deepcopy(original.correlation)
        other["priority"] = 200
        preview = self.service.preview(self.candidate())
        self.assertEqual(preview["correlation_impact"]["affected_open_incidents"], 1)
        self.apply()
        self.correlate(self.event("p-2", service="same"))
        self.assertEqual({row.correlation_context["rule_id"] for row in self.incidents()}, {"workload", "service"})
        self.correlate(original, save=False)
        self.assertEqual(original.correlation, evidence)
        self.assertEqual(next(row for row in self.incidents() if row.id == old.id).alerts_count, 1)

    def test_group_by_update_preview_reports_old_groups_and_pins_history(self):
        original = self.event()
        self.correlate(original)
        old = self.incidents()[0]
        rule = self.bundle["correlation"][0]
        rule["group_by"] = ["normalized.cluster", "normalized.resource"]
        rule["required_fields"] = rule["group_by"]
        preview = self.service.preview(self.candidate())
        self.assertEqual(preview["correlation_impact"]["affected_open_incidents"], 1)
        self.assertFalse(preview["correlation_impact"]["rewrite_history"])
        self.apply()
        self.correlate(self.event("p-2"))
        self.assertEqual(len(self.incidents()), 2)
        retained = next(row for row in self.incidents() if row.id == old.id)
        self.assertEqual(retained.correlation_context, old.correlation_context)

    def test_non_matching_and_broken_cel_are_saved_with_diagnostics(self):
        self.bundle["correlation"][0]["match"] = "labels.absent == 'x'"
        self.apply()
        event = self.event()
        self.correlate(event)
        self.assertEqual(event.correlation["decisions"][0]["reason"], "match_evaluation_error")
        self.bundle["correlation"][0]["match"] = "normalized.kind == 'pvc'"
        self.apply()
        event = self.event("p-2")
        self.correlate(event)
        self.assertEqual(event.correlation["decisions"][0]["reason"], "no_matching_rule")
        self.assertEqual(self.incidents(), [])

    def test_missing_winning_rule_does_not_fall_through_to_broader_rule(self):
        self.second_rule()
        self.apply()
        self.correlate(self.event(service="same", workload=None))
        self.assertEqual(self.incidents(), [])

    def test_create_on_all_collects_or_branches_across_distinct_firing_alerts(self):
        rule = self.bundle["correlation"][0]
        rule.update(create_on="all", threshold=2, match="((labels.problem == 'cpu') || (labels.problem == 'memory'))")
        self.apply()
        self.correlate(self.event(problem="cpu"))
        self.correlate(self.event("p-2", problem="cpu"), 1)
        self.assertFalse(self.incidents()[0].is_visible)
        self.correlate(self.event("p-3", problem="memory"), 2)
        self.assertTrue(self.incidents()[0].is_visible)
        rule["match"] = "true ? (labels.problem == 'cpu') : (labels.problem == 'memory')"
        with self.assertRaisesRegex(ValueError, "all requires boolean OR"):
            self.candidate()

    def test_correlation_does_not_link_a_previous_episode_from_another_team(self):
        self.correlate(self.event())
        with Session(self.engine) as session:
            row = session.exec(select(Incident)).one()
            row.team_id = "beta"
            session.add(row)
            session.commit()
        self.correlate(self.event("p-2"), 1)
        row = next(row for row in self.incidents() if row.team_id == "alpha")
        self.assertIsNone(row.same_incident_in_the_past_id)

    def test_multi_level_keys_are_unique_and_invalid_entry_is_explicit(self):
        rule = self.bundle["correlation"][0]
        rule.update(group_by=["labels.customers"], required_fields=["labels.customers"],
                    multi_level=True, multi_level_property_name="name")
        self.apply()
        self.correlate(self.event(customers={"one": {"name": "Alice"}, "two": {"name": "Bob"}, "copy": {"name": "Alice"}}))
        self.assertEqual(len(self.incidents()), 2)
        event = self.event("p-2", customers={"broken": None})
        self.correlate(event)
        self.assertEqual(event.correlation["decisions"][0]["missing_fields"][0]["state"], "missing_multilevel_value")
        self.assertEqual(len(self.incidents()), 2)

    def test_legacy_multi_level_missing_and_delimiters_are_safe(self):
        engine = RulesEngine("tenant")
        rule = Rule(grouping_criteria=["labels.customers"], multi_level=True, multi_level_property_name="name")
        self.assertEqual(engine._calc_rule_fingerprint(self.event(customers=None), rule), [])
        self.assertEqual(engine._calc_rule_fingerprint(self.event(customers={"bad": None}), rule), [])
        rule.multi_level = False
        rule.grouping_criteria = ["labels.a", "labels.b"]
        self.assertNotEqual(engine._calc_rule_fingerprint(self.event(a="a,b", b="c"), rule),
                            engine._calc_rule_fingerprint(self.event(a="a", b="b,c"), rule))

    def test_resolve_policies_use_all_canonical_members_and_new_episode(self):
        self.correlate(self.event())
        self.correlate(self.event("p-2"), 1)
        self.correlate(self.event(status="resolved"), 2)
        self.assertEqual(self.incidents()[0].status, "firing")
        self.correlate(self.event("p-2", status="resolved"), 3)
        self.assertEqual(self.incidents()[0].status, "resolved")
        old = self.incidents()[0]
        self.correlate(self.event(), 4)
        new = next(row for row in self.incidents() if row.id != old.id)
        self.assertEqual(new.same_incident_in_the_past_id, old.id)
        self.assertIsNone(new.assignee)

    def test_never_and_first_resolved_are_configurable(self):
        for policy in ("never", "first_resolved", "last_resolved"):
            self.bundle["lifecycle"][0]["resolve_on"] = policy
            self.apply()
            self.correlate(self.event(policy + "-1"), 0)
            self.correlate(self.event(policy + "-2"), 1)
            self.correlate(self.event(policy + "-1", status="resolved"), 2)
            row = next(row for row in self.incidents() if row.correlation_context["lifecycle"]["resolve_on"] == policy)
            self.assertEqual(row.status, "resolved" if policy == "first_resolved" else "firing")

    def test_late_version_and_untrusted_projection_cannot_change_membership(self):
        first = self.event()
        self.save(first)
        self.save(self.event(), 1)
        self.correlate(first, save=False)
        self.assertEqual(first.correlation["decisions"][0]["reason"], "history_only")
        self.assertEqual(self.incidents(), [])
        event = self.event("p-2")
        self.save(event, 2)
        event.normalized["workload"] = "forged"
        self.correlate(event, save=False)
        self.assertEqual(self.incidents()[0].correlation_context["group_values"][-1][-1], ["string", "catalog"])

    def test_manual_notes_and_assignee_and_unlink_survive_repeat(self):
        import keep.api.core.db as db
        self.correlate(self.event())
        identifier = self.incidents()[0].id
        with Session(self.engine) as session:
            row = session.get(Incident, identifier)
            row.user_generated_name, row.user_summary, row.assignee = "Manual", "Notes", "engineer"
            session.add(row)
            session.commit()
        self.correlate(self.event("p-2"), 1)
        db.remove_alerts_to_incident_by_incident_id("tenant", identifier, ["p-1"])
        event = self.event()
        self.correlate(event, 2)
        self.assertEqual(event.correlation["decisions"][0]["reason"], "manually_unlinked")
        row = self.incidents()[0]
        self.assertEqual((row.user_generated_name, row.user_summary, row.assignee), ("Manual", "Notes", "engineer"))
        self.assertEqual(row.alerts_count, 1)

    def test_rule_presentation_and_explanation_are_returned_by_dto(self):
        self.correlate(self.event())
        with Session(self.engine) as session:
            row = session.exec(select(Incident)).one()
            dto = IncidentDto.from_db_incident(row, session=session)
            self.assertEqual(dto.presentation["title"], "catalog")
            self.assertEqual(dto.correlation["rule_id"], "workload")
            self.assertEqual(dto.correlation["policy"]["window_seconds"], 60)
            from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
            alert = session.exec(select(Alert)).one()
            projection = convert_db_alerts_to_dto_alerts([alert], with_silences=False, session=session)[0]
            self.assertEqual(projection.correlation["decisions"][0]["incident_id"], str(row.id))

    def test_future_automation_rejected_without_changing_active_snapshot(self):
        baseline = self.service.status()["active_digest"]
        self.bundle["automation"] = [{"id": "future"}]
        with self.assertRaises(ValueError):
            self.candidate()
        self.assertEqual(self.service.status()["active_digest"], baseline)

    def test_transaction_failure_rolls_back_group_incident_link_and_evidence(self):
        from keep.api.models.db.alert import LastAlertToIncident
        from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
        event = self.event()
        self.save(event)
        with patch("keep.api.core.incident_correlation.refresh_incident_presentation", side_effect=RuntimeError("fixture failure")):
            with self.assertRaises(RuntimeError):
                self.correlate(event, save=False)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Incident)).all(), [])
            self.assertEqual(session.exec(select(IncidentCorrelationGroup)).all(), [])
            self.assertEqual(session.exec(select(LastAlertToIncident)).all(), [])
            self.assertIsNone(session.get(Alert, UUID(event.id)).correlation_context)
        self.correlate(event, save=False)
        self.assertEqual(len(self.incidents()), 1)

    def test_correlation_explanation_does_not_change_deduplication(self):
        from keep.api.core.event_normalization import deduplication_payload
        event = self.event()
        original = deduplication_payload(event)
        event.correlation = {"decisions": [{"rule_version": "changed", "reason": "matched"}]}
        self.assertEqual(deduplication_payload(event), original)

    def test_iac_policy_and_evidence_cannot_be_overwritten_through_incident_update(self):
        import keep.api.core.db as db
        from fastapi import HTTPException
        from keep.api.models.incident import IncidentDtoIn
        self.correlate(self.event())
        row = self.incidents()[0]
        with self.assertRaises(HTTPException) as error:
            db.update_incident_from_dto_by_id("tenant", row.id, IncidentDtoIn(resolve_on="never"))
        self.assertEqual(error.exception.status_code, 409)
        db.update_incident_from_dto_by_id("tenant", row.id, IncidentDtoIn(user_generated_name="Manual", correlation_context={"rule_id": "forged"}))
        row = self.incidents()[0]
        self.assertEqual(row.user_generated_name, "Manual")
        self.assertEqual(row.correlation_context["rule_id"], "workload")

    def test_independent_pvc_node_and_service_objects_do_not_join_by_service(self):
        rule = self.bundle["correlation"][0]
        rule["match"] = "true"
        rule["group_by"] = ["normalized.kind", "normalized.cluster", "normalized.resource"]
        rule["required_fields"] = rule["group_by"]
        self.apply()
        for kind in ("pvc", "node", "service"):
            self.correlate(self.event(kind + "-1", kind=kind, resource="same", service="catalog"))
            self.correlate(self.event(kind + "-2", kind=kind, resource="other", service="catalog"))
        self.assertEqual(len(self.incidents()), 6)

    def test_full_ingestion_uses_canonical_team_and_stores_rule_evidence(self):
        import keep.api.tasks.process_event_task as task
        mapping = {"name": "ownership", "matchers": [["labels.namespace"]],
                   "rows": [{"labels.namespace": "north", "zone": "ALPHA"}, {"labels.namespace": "south", "zone": "BETA"}]}
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", mapping)
        self.apply()
        with patch.object(task.WorkflowManager, "get_instance"), patch.object(task, "KEEP_CALCULATE_START_FIRING_TIME_ENABLED", False):
            for pod, namespace in (("p-1", "north"), ("p-2", "north"), ("p-1", "south")):
                raw = {"name": pod, "labels": {"resource": pod, "cluster": "lab", "namespace": namespace, "kind": "workload", "workload": "catalog"},
                       "team_id": "forged", "correlation": {"decisions": [{"reason": "forged"}]}}
                task.process_event({}, "tenant", "keep", None, None, None, "fixture", raw, notify_client=False)
        self.assertEqual(len(self.incidents()), 2)
        self.assertEqual(sorted(row.alerts_count for row in self.incidents()), [1, 2])
        with Session(self.engine) as session:
            self.assertTrue(all(row.correlation_context["decisions"][0]["reason"] == "matched" for row in session.exec(select(Alert)).all()))
