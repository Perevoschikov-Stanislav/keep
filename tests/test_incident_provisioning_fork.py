"""Safe provisioning must preserve the last configuration and manual resources."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from sqlmodel import Session, select

import keep.api.core.db as db
from keep.api.models.db.mapping import MappingRule
from keep.api.models.db.workflow import Workflow
from keep.api.models.db.incident import Incident
from tests.team_fork_test_case import TeamDatabaseTestCase, POLICY


class LegacyProvisioningTest(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"KEEP_MAPPINGS_DIRECTORY": str(self.directory)}))
        self.manifest = {"name": "example", "matchers": [["namespace"]],
                         "rows": [{"namespace": "monitoring", "service": "metrics"}]}
        (self.directory / "a.yaml").write_text(yaml.safe_dump(self.manifest))

    def provision(self):
        from keep.api.bl.mapping_rules_provisioning import provision_mapping_rules_from_env
        provision_mapping_rules_from_env("tenant")

    def rules(self):
        with Session(self.engine) as session:
            return session.exec(select(MappingRule)).all()

    def test_missing_environment_retains_mappings_and_workflows(self):
        self.provision()
        with Session(self.engine) as session:
            session.add(Workflow(id="retained", tenant_id="tenant", name="retained",
                                 created_by="system", workflow_raw="{}", provisioned=True))
            session.commit()
        with patch.dict(os.environ, {"KEEP_MAPPINGS_DIRECTORY": "", "KEEP_WORKFLOWS_DIRECTORY": "", "KEEP_WORKFLOW": ""}):
            self.provision()
            from keep.workflowmanager.workflowstore import WorkflowStore
            WorkflowStore.provision_workflows("tenant")
        self.assertEqual(len(self.rules()), 1)
        with Session(self.engine) as session:
            self.assertFalse(session.get(Workflow, "retained").is_deleted)

    def test_missing_manifest_retains_old_mapping(self):
        self.provision()
        (self.directory / "a.yaml").unlink()
        self.provision()
        self.assertEqual(len(self.rules()), 1)

    def test_last_invalid_file_rolls_back_entire_directory(self):
        self.provision()
        changed = {**self.manifest, "priority": 42}
        (self.directory / "a.yaml").write_text(yaml.safe_dump(changed))
        (self.directory / "z.yaml").write_text("name: [broken")
        with self.assertRaises(ValueError):
            self.provision()
        self.assertEqual(self.rules()[0].priority, 0)

    def test_same_name_ui_resource_is_not_adopted(self):
        with Session(self.engine) as session:
            session.add(MappingRule(tenant_id="tenant", name="example", priority=77,
                                    matchers=[["namespace"]], rows=self.manifest["rows"]))
            session.commit()
        with self.assertRaises(ValueError):
            self.provision()
        self.assertFalse(self.rules()[0].is_provisioned)
        self.assertEqual(self.rules()[0].priority, 77)

    def test_workflow_directory_is_atomic_and_idempotent(self):
        from keep.workflowmanager.workflowstore import WorkflowStore
        directory = self.directory / "workflows"
        directory.mkdir()
        document = {"workflow": {"id": "record", "triggers": [{"type": "manual"}],
                                  "actions": [{"name": "record", "provider": {"type": "mock", "with": {"value": "initial"}}}]}}
        path = directory / "a.yaml"
        path.write_text(yaml.safe_dump(document))
        with patch.dict(os.environ, {"KEEP_WORKFLOW": "", "KEEP_WORKFLOWS_DIRECTORY": str(directory)}):
            WorkflowStore.provision_workflows("tenant")
            with Session(self.engine) as session:
                initial = session.exec(select(Workflow)).one()
                initial_id, initial_revision, initial_raw = initial.id, initial.revision, initial.workflow_raw
            WorkflowStore.provision_workflows("tenant")
            with Session(self.engine) as session:
                self.assertEqual(session.get(Workflow, initial_id).revision, initial_revision)
            document["workflow"]["actions"][0]["provider"]["with"]["value"] = "changed"
            path.write_text(yaml.safe_dump(document))
            (directory / "z.yaml").write_text("workflow: [broken")
            with self.assertRaises(ValueError):
                WorkflowStore.provision_workflows("tenant")
            with Session(self.engine) as session:
                self.assertEqual(session.get(Workflow, initial_id).workflow_raw, initial_raw)

    def test_legacy_workflow_and_mapping_share_rollback_boundary(self):
        from keep.workflowmanager.workflowstore import WorkflowStore
        workflow = {"workflow": {"id": "record", "triggers": [{"type": "manual"}],
                                 "actions": [{"name": "record", "provider": {"type": "mock", "with": {"value": "initial"}}}]}}
        (self.directory / "z.yaml").write_text("name: [broken")
        with patch.dict(os.environ, {"KEEP_WORKFLOW": yaml.safe_dump(workflow), "KEEP_WORKFLOWS_DIRECTORY": ""}):
            with self.assertRaises(ValueError):
                with Session(self.engine) as session, session.begin():
                    WorkflowStore.provision_workflows("tenant", session=session)
                    from keep.api.bl.mapping_rules_provisioning import provision_mapping_rules_from_env
                    provision_mapping_rules_from_env("tenant", session=session)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Workflow)).all(), [])


class IncidentProvisioningCase(TeamDatabaseTestCase):
    def setUp(self):
        # Import before SQLModel.metadata.create_all so new metadata is included.
        from keep.api.bl.incident_provisioning import IncidentProvisioning
        self.service_class = IncidentProvisioning
        super().setUp()
        self.service = IncidentProvisioning("tenant")
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mapping = {"name": "display name", "matchers": [["namespace"]],
                        "rows": [{"namespace": "monitoring", "service": "metrics"}]}
        self.bundle = {"api_version": "keep.incidents/v1", "kind": "IncidentPolicies",
                       "id": "test", "tenant_id": "tenant", "revision": "v1",
                       "keep_url": "https://keep.example.test",
                       "access": self.artifact("teams.yaml", yaml.safe_load(POLICY)),
                       "mappings": [{"id": "stable", "artifact": self.artifact("mapping.yaml", self.mapping)}]}

    def artifact(self, filename, document):
        content = yaml.safe_dump(document).encode()
        (self.directory / filename).write_bytes(content)
        return {"path": filename, "sha256": hashlib.sha256(content).hexdigest()}

    def candidate(self):
        from keep.api.bl.incident_provisioning import Candidate
        return Candidate.load(self.bundle, self.directory, "tenant")

    def apply(self, candidate=None, preview=None):
        candidate = candidate or self.candidate()
        preview = preview or self.service.preview(candidate)
        return self.service.apply(candidate, expected_active_digest=preview["active_digest"],
                                  expected_candidate_digest=preview["candidate_digest"],
                                  expected_preview_digest=preview["preview_digest"], actor="test-admin")



class IncidentProvisioningTest(IncidentProvisioningCase):
    def test_preview_is_read_only_and_repeated_apply_is_noop(self):
        candidate = self.candidate()
        preview = self.service.preview(candidate)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(MappingRule)).all(), [])
        self.assertIsNone(self.service.status()["active_digest"])
        first = self.apply(candidate, preview)
        status = self.service.status()
        second = self.apply(candidate)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(second["result"], "noop")
        self.assertEqual(status, self.service.status())

    def test_startup_retains_active_snapshot_when_bundle_mount_is_absent_or_invalid(self):
        from keep.api import config
        from keep.api.core.incident_configuration import configuration_scope, reset_configuration_cache
        from keep.identitymanager.team_policy import get_team_policy
        self.apply()
        before = self.service.status()
        bad = self.directory / "invalid-bundle.yaml"
        bad.write_text("kind: [invalid")
        with patch.object(config, "SINGLE_TENANT_UUID", "tenant"), patch.object(config, "PROVISION_RESOURCES", True), \
             patch.object(config.ProvidersService, "provision_providers"), patch.object(config, "provision_dashboards"), \
             patch.object(config, "provision_deduplication_rules_from_env"), patch.object(config, "provision_mapping_rules_from_env") as legacy:
            for source in ("", str(self.directory / "missing.yaml"), str(bad)):
                with patch.dict(os.environ, {"KEEP_INCIDENT_POLICIES_CONFIG_FILE": source}):
                    reset_configuration_cache()
                    config.provision_resources()
                    self.assertEqual(self.service.status(), before)
                    with configuration_scope("tenant"):
                        self.assertEqual(set(get_team_policy().teams), {"alpha", "beta"})
            legacy.assert_not_called()

    def test_runtime_examples_validate_with_different_team_ids_and_native_parameters(self):
        from keep.api.bl.incident_provisioning import Candidate
        root = Path(__file__).resolve().parents[1] / "config/incident-policies.example"
        checked = [Candidate.from_file(root / name / "bundle.yaml", "keep") for name in ("a", "b")]
        self.assertNotEqual(checked[0].digest, checked[1].digest)
        for item in checked:
            self.assertEqual({resource["kind"] for resource in item.resources}, {"teams", "mappings", "extraction", "rules", "workflows"})
        self.assertTrue(set(team["id"] for team in checked[0].documents["access"]["teams"]).isdisjoint(
            team["id"] for team in checked[1].documents["access"]["teams"]))
        self.assertNotEqual(checked[0].resources[-2]["data"]["timeframe"], checked[1].resources[-2]["data"]["timeframe"])

    def test_rename_retains_internal_identity(self):
        self.apply()
        with Session(self.engine) as session:
            before = session.exec(select(MappingRule)).one().id
        self.mapping["name"] = "renamed"
        self.bundle["mappings"][0]["artifact"] = self.artifact("renamed.yaml", self.mapping)
        self.assertFalse(self.service.preview(self.candidate())["changes"][-1]["drift"])
        self.apply()
        with Session(self.engine) as session:
            rule = session.exec(select(MappingRule)).one()
            self.assertEqual(rule.id, before)
            self.assertEqual(rule.name, "renamed")

    def test_omission_requires_explicit_deletion(self):
        self.apply()
        self.bundle["mappings"] = []
        with self.assertRaises(ValueError):
            self.service.preview(self.candidate())
        self.assertIsNotNone(self.service.status()["active_digest"])

    def test_foreign_tenant_and_unimplemented_policy_are_rejected(self):
        self.bundle["tenant_id"] = "foreign"
        with self.assertRaises(ValueError):
            self.candidate()
        self.bundle["tenant_id"] = "tenant"
        self.bundle["normalization"] = [{"id": "future"}]
        with self.assertRaises(ValueError):
            self.candidate()

    def test_losing_apply_cannot_overwrite_newer_version(self):
        original = self.candidate()
        stale = self.service.preview(original)
        self.mapping["priority"] = 10
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        current = self.candidate()
        self.apply(current)
        with self.assertRaises(ValueError):
            self.apply(original, stale)
        self.assertEqual(self.service.status()["active_digest"], current.digest)

    def test_manual_collision_is_visible_in_preview(self):
        with Session(self.engine) as session:
            session.add(MappingRule(tenant_id="tenant", **self.mapping))
            session.commit()
        with self.assertRaises(ValueError):
            self.service.preview(self.candidate())

    def test_explicit_adoption_keeps_id_and_repeated_apply_is_noop(self):
        target = self.adopt_mapping()
        self.assertEqual(self.service.preview(self.candidate())["changes"][-1]["operation"], "adopt")
        self.apply()
        self.assertEqual(self.apply()["result"], "noop")
        with Session(self.engine) as session:
            self.assertEqual(str(session.exec(select(MappingRule)).one().id), target)

    def adopt_mapping(self):
        from keep.api.bl.incident_provisioning import current_values, digest
        with Session(self.engine) as session:
            row = MappingRule(tenant_id="tenant", **self.mapping)
            session.add(row)
            session.commit()
            target = str(row.id)
            resource_digest = digest(current_values("mappings", row))
        self.bundle["adoptions"] = [{"kind": "mappings", "id": "stable", "target_id": target,
                                     "expected_resource_digest": resource_digest}]
        return target

    def test_explicit_deletion_retires_configuration_and_is_repeatable(self):
        self.apply()
        owned = next(item for item in self.service.status()["resources"] if item["kind"] == "mappings")
        self.bundle["mappings"] = []
        self.bundle["deletions"] = [{"kind": "mappings", "id": "stable", "expected_resource_digest": owned["digest"]}]
        self.apply()
        self.assertEqual(self.apply()["result"], "noop")
        with Session(self.engine) as session:
            self.assertTrue(session.exec(select(MappingRule)).one().disabled)
        self.assertFalse(any(item["kind"] == "mappings" for item in self.service.status()["resources"]))

    def test_mid_transaction_failure_preserves_entire_previous_snapshot(self):
        self.apply()
        previous = self.service.status()
        self.bundle["mappings"].append({"id": "second", "artifact": self.artifact("second.yaml", {**self.mapping, "name": "second"})})
        self.mapping["priority"] = 7
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        writer = self.service._write_resource
        written = []
        def fail_after_first(*args, **kwargs):
            if written:
                raise RuntimeError("injected write failure")
            result = writer(*args, **kwargs)
            written.append(True)
            return result
        with patch.object(self.service, "_write_resource", side_effect=fail_after_first):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.service.status(), previous)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(MappingRule)).one().priority, 0)

    def test_one_operation_uses_one_version_and_next_operation_reloads(self):
        from keep.api.core.incident_configuration import configuration_scope, configured_resources
        from keep.identitymanager.team_policy import get_team_policy
        self.apply()
        with configuration_scope("tenant"):
            self.mapping["priority"] = 100
            self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
            policy = yaml.safe_load(POLICY)
            policy["teams"].append({"id": "gamma", "groups": ["/teams/gamma"], "zones": ["GAMMA"]})
            self.bundle["access"] = self.artifact("teams.yaml", policy)
            self.apply()
            self.assertNotIn("gamma", get_team_policy().teams)
            with Session(self.engine) as session:
                rules = configured_resources(session, "tenant", "mappings", MappingRule, session.exec(select(MappingRule)).all())
                self.assertEqual(rules[0].priority, 0)
        with configuration_scope("tenant"):
            self.assertIn("gamma", get_team_policy().teams)
            with Session(self.engine) as session:
                rules = configured_resources(session, "tenant", "mappings", MappingRule, session.exec(select(MappingRule)).all())
                self.assertEqual(rules[0].priority, 100)

    def test_restore_reads_saved_artifacts_and_preserves_manual_incident(self):
        self.apply()
        self.mapping["priority"] = 100
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        self.apply()
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", user_summary="manual note", assignee="engineer@example.test")
            session.add(incident)
            session.commit()
            incident_id = incident.id
        for artifact in self.directory.iterdir():
            artifact.unlink()
        restored = self.service.restore_candidate(1)
        self.apply(restored)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(MappingRule)).one().priority, 0)
            self.assertEqual(session.get(Incident, incident_id).user_summary, "manual note")
            self.assertEqual(session.get(Incident, incident_id).assignee, "engineer@example.test")

    def test_restore_requires_reviewed_deletion_for_resources_added_later(self):
        self.apply()
        self.bundle["mappings"].append({"id": "later", "artifact": self.artifact("later.yaml", {**self.mapping, "name": "later"})})
        self.apply()
        restored = self.service.restore_candidate(1)
        with self.assertRaisesRegex(ValueError, "omitted without explicit deletion"):
            self.service.preview(restored)
        later = next(item for item in self.service.status()["resources"] if item["id"] == "later")
        deletions = [{"kind": "mappings", "id": "later", "expected_resource_digest": later["digest"]}]
        self.apply(self.service.restore_candidate(1, deletions=deletions))
        with Session(self.engine) as session:
            rows = session.exec(select(MappingRule)).all()
            self.assertEqual(sum(not row.disabled for row in rows), 1)
            self.assertTrue(next(row for row in rows if row.name == "later").disabled)

    def test_restore_retired_adoption_preserves_binding(self):
        self.adopt_mapping()
        self.apply()
        owned = next(item for item in self.service.status()["resources"] if item["kind"] == "mappings")
        self.bundle["mappings"] = []
        self.bundle["adoptions"] = []
        self.bundle["deletions"] = [{"kind": "mappings", "id": "stable", "expected_resource_digest": owned["digest"]}]
        self.apply()
        self.apply(self.service.restore_candidate(1))
        with Session(self.engine) as session:
            row = session.exec(select(MappingRule)).one()
            self.assertEqual(str(row.id), owned["target_id"])
            self.assertFalse(row.disabled)

    def test_team_with_history_cannot_be_removed(self):
        self.apply()
        with Session(self.engine) as session:
            session.add(Incident(tenant_id="tenant", team_id="alpha"))
            session.commit()
        alpha = next(item for item in self.service.status()["resources"] if item["kind"] == "teams" and item["id"] == "alpha")
        policy = yaml.safe_load(POLICY)
        policy["teams"] = [item for item in policy["teams"] if item["id"] != "alpha"]
        self.bundle["access"] = self.artifact("teams.yaml", policy)
        self.bundle["deletions"] = [{"kind": "teams", "id": "alpha", "expected_resource_digest": alpha["digest"]}]
        with self.assertRaisesRegex(ValueError, "ownership migration"):
            self.service.preview(self.candidate())

    def test_changed_files_since_preview_require_new_preview(self):
        preview = self.service.preview(self.candidate())
        self.mapping["priority"] = 7
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.apply(self.candidate(), preview)

    def test_drift_is_reported_and_repair_requires_new_preview(self):
        self.apply()
        checked = self.candidate()
        stale = self.service.preview(checked)
        with Session(self.engine) as session:
            row = session.exec(select(MappingRule)).one()
            row.priority = 444
            session.add(row)
            session.commit()
        self.assertEqual(self.service.status()["drift"], [{"kind": "mappings", "id": "stable"}])
        with self.assertRaisesRegex(ValueError, "resource state changed"):
            self.apply(checked, stale)
        self.apply(checked)
        self.assertEqual(self.service.status()["drift"], [])

    def test_provisioning_flags_and_provenance_drift_are_repaired(self):
        self.apply()
        with Session(self.engine) as session:
            row = session.exec(select(MappingRule)).one()
            row.is_provisioned, row.provisioned_file = False, "out-of-band.yaml"
            session.add(row)
            session.commit()
        self.assertEqual(self.service.status()["drift"], [{"kind": "mappings", "id": "stable"}])
        self.apply()
        self.assertEqual(self.service.status()["drift"], [])
        with Session(self.engine) as session:
            row = session.exec(select(MappingRule)).one()
            self.assertTrue(row.is_provisioned)
            self.assertEqual(row.provisioned_file, "mapping.yaml")

    def native_resources(self):
        extraction = {"name": "component extraction", "attribute": "name", "regex": "(?P<component>.+)"}
        rule = {"ruleName": "existing correlation", "sqlQuery": {"sql": "(1 = 1)", "params": {}},
                "celQuery": "true", "timeframeInSeconds": 120, "timeUnit": "seconds"}
        workflow = {"workflow": {"id": "evidence", "name": "evidence", "triggers": [{"type": "manual"}],
                                  "actions": [{"name": "record", "notification": False,
                                               "provider": {"type": "mock", "with": {"value": "evidence"}}}]}}
        for kind, logical_id, document in (("extraction", "extract", extraction), ("rules", "correlate", rule), ("workflows", "evidence", workflow)):
            self.bundle[kind] = [{"id": logical_id, "artifact": self.artifact(kind + ".yaml", document)}]

    def test_native_formats_are_provisioned_and_protected_in_existing_api(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from keep.api.routes import extraction, mapping, rules, workflows
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        self.native_resources()
        self.apply()
        before = self.service.status()
        app = FastAPI()
        for module, prefix in ((mapping, "/mapping"), (extraction, "/extraction"), (rules, "/rules"), (workflows, "/workflows")):
            app.include_router(module.router, prefix=prefix)
        entity = AuthenticatedEntity(tenant_id="tenant", email="admin@example.test", role="admin")
        for route in app.routes:
            for dependency in getattr(getattr(route, "dependant", None), "dependencies", []):
                if dependency.name == "authenticated_entity":
                    app.dependency_overrides[dependency.call] = lambda: entity
        client = self.enterContext(TestClient(app))
        from keep.api.routes.rules import RuleCreateDto
        payloads = {"mappings": self.mapping,
                    "extraction": yaml.safe_load((self.directory / "extraction.yaml").read_text()),
                    "rules": RuleCreateDto(**yaml.safe_load((self.directory / "rules.yaml").read_text())).dict(),
                    "workflows": {}}
        for item in before["resources"]:
            if not item["target_id"]:
                continue
            prefix = "mapping" if item["kind"] == "mappings" else item["kind"]
            url = "/" + prefix + "/" + item["target_id"]
            for method in ("put", "delete"):
                with self.subTest(kind=item["kind"], method=method):
                    result = client.put(url, json=payloads[item["kind"]]) if method == "put" else client.delete(url)
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assertEqual(result.json()["detail"]["code"], "iac_managed_resource")
            if item["kind"] == "workflows":
                self.assertEqual(client.put(url + "/toggle").status_code, 409)
        self.assertEqual(self.service.status(), before)
        self.assertEqual(self.apply()["result"], "noop")

    def test_delivery_reloads_transport_and_revoked_subscriber_before_send(self):
        from datetime import datetime
        from uuid import uuid4
        from unittest.mock import Mock
        from keep.api.bl.silences_delivery_bl import SilenceDeliveryWorker
        from keep.api.core.incident_configuration import active_configuration
        from keep.api.core.silence_integrations import SilenceIntegrations
        from keep.api.models.db.silence import NotificationDelivery
        self.bundle["transports"] = [{"id": "webhook", "kind": "http_json", "adapter_ref": "http-json-v1",
            "endpoint": "http://127.0.0.1:9000", "auth_ref": None,
            "capabilities": {"update": False, "actions": False, "receipts": False}}]
        self.bundle["destinations"] = [{"id": "alpha-hook", "team_id": "alpha", "transport_ref": "webhook", "options": {"path": "/events"}}]
        self.bundle["subscribers"] = [{"id": "alpha-hook", "team_ids": ["alpha"], "event_types": ["silence.created"], "destination_refs": ["alpha-hook"]}]
        self.apply()
        settings = SilenceIntegrations.from_snapshot(active_configuration("tenant"))
        now = datetime(2026, 10, 5, 8)
        sender = Mock(return_value=200)
        worker = SilenceDeliveryWorker(self.engine, settings, clock=lambda: now, sender=sender)
        def enqueue():
            with Session(self.engine) as session:
                session.add(NotificationDelivery(tenant_id="tenant", team_id="alpha", event_id=uuid4(),
                    subscriber_id="alpha-hook", destination_id="alpha-hook", transport_id="webhook",
                    policy_digest=settings.digest, payload={"event_type": "silence.created"},
                    available_at=now, created_at=now))
                session.commit()
        enqueue()
        claimed = worker.claim()
        self.bundle["transports"][0]["endpoint"] = "http://127.0.0.1:9001"
        self.apply()
        self.assertTrue(worker.send_claimed(claimed))
        self.assertEqual(sender.call_args.args[1]["endpoint"], "http://127.0.0.1:9001")
        enqueue()
        claimed = worker.claim()
        subscriber = next(item for item in self.service.status()["resources"] if item["kind"] == "subscribers")
        self.bundle["subscribers"] = []
        self.bundle["deletions"] = [{"kind": "subscribers", "id": "alpha-hook", "expected_resource_digest": subscriber["digest"]}]
        self.apply()
        self.assertFalse(worker.send_claimed(claimed))
        self.assertEqual(sender.call_count, 1)
        with Session(self.engine) as session:
            self.assertEqual(session.get(NotificationDelivery, claimed.id).state, "disabled")

    def test_long_lived_auth_verifier_reloads_roles_and_membership(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        from keep.api.core.incident_configuration import configuration_scope
        from keep.identitymanager.identity_managers.oauth2proxy import oauth2proxy_authverifier as auth
        self.apply()
        verifier = auth.Oauth2proxyAuthVerifier(["read:settings"])
        def authenticate(groups):
            request = Request({"type": "http", "headers": [(b"x-forwarded-email", b"operator@example.test"),
                                                           (b"x-forwarded-groups", groups.encode())]})
            return verifier.authenticate(request, None, None, None)
        with patch.object(auth, "user_exists", return_value=False), patch.object(auth, "create_user"):
            with configuration_scope("tenant"):
                self.assertEqual(authenticate("/roles/viewer,/teams/alpha").role, "viewer")
            policy = yaml.safe_load(POLICY)
            policy["roles"]["viewer"] = ["/roles/reader"]
            policy["teams"][0]["groups"] = ["/teams/new-alpha"]
            self.bundle["access"] = self.artifact("teams.yaml", policy)
            self.apply()
            with configuration_scope("tenant"):
                with self.assertRaises(HTTPException) as failure:
                    authenticate("/roles/viewer,/teams/alpha")
                self.assertEqual(failure.exception.status_code, 403)
                entity = authenticate("/roles/reader,/teams/new-alpha")
                self.assertEqual(entity.teams, frozenset({"alpha"}))

    def test_two_arbitrary_team_configurations_change_membership_and_visibility(self):
        from keep.api.core.incident_configuration import configuration_scope
        from keep.identitymanager.team_policy import get_team_policy
        for names, visibility in ((["platform-blue", "database-green"], "team"), (["frontend-west", "network-east"], "all")):
            # Use a fresh tenant for each independent bundle; no omission/deletion ambiguity.
            tenant_id = "tenant-" + names[0]
            from keep.api.models.db.tenant import Tenant
            with Session(self.engine) as session:
                session.add(Tenant(id=tenant_id, name=tenant_id))
                session.commit()
            self.service = self.service_class(tenant_id)
            self.bundle["tenant_id"] = tenant_id
            policy = yaml.safe_load(POLICY)
            policy["visibility"] = visibility
            policy["teams"] = [{"id": name, "groups": ["/membership/" + name], "zones": [name + "-zone"]} for name in names]
            self.bundle["access"] = self.artifact("teams.yaml", policy)
            candidate = self.candidate_for_tenant(tenant_id)
            self.apply(candidate)
            with configuration_scope(tenant_id):
                active = get_team_policy()
                self.assertEqual(active.teams_for_groups({"/membership/" + names[0]}), frozenset({names[0]}))
                self.assertEqual(active.team_for_zone(names[1] + "-zone"), names[1])
                self.assertEqual(active.visibility, visibility)

    def candidate_for_tenant(self, tenant_id):
        from keep.api.bl.incident_provisioning import Candidate
        return Candidate.load(self.bundle, self.directory, tenant_id)

    def test_artifact_digest_and_duplicate_yaml_are_rejected_before_any_write(self):
        (self.directory / "mapping.yaml").write_text("name: bad\nname: duplicate\n")
        with self.assertRaises(ValueError):
            self.candidate()
        content = (self.directory / "mapping.yaml").read_bytes()
        self.bundle["mappings"][0]["artifact"]["sha256"] = hashlib.sha256(content).hexdigest()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.candidate()
        self.assertIsNone(self.service.status()["active_digest"])

    def test_unimplemented_adapter_profile_is_not_activated(self):
        from keep.api.bl.incident_provisioning import Candidate
        from keep.api.core.incident_contract import read_yaml
        example = Path(__file__).resolve().parents[1] / "docs/fork/incident-core/examples/a/bundle.yaml"
        document = read_yaml(example)
        with self.assertRaisesRegex(ValueError, "implemented adapter profile"):
            Candidate.load(document, example.parent, document["tenant_id"])

    def test_queued_step_with_old_config_cannot_call_provider(self):
        from unittest.mock import Mock
        from keep.contextmanager.contextmanager import ContextManager
        from keep.step.step import Step, StepType
        self.apply()
        context = ContextManager("tenant")
        context.configuration_digest = self.service.status()["active_digest"]
        provider = Mock()
        step = Step(context, "queued", {}, StepType.ACTION, provider, {})
        self.mapping["priority"] = 20
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        self.apply()
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            step.run()
        provider.notify.assert_not_called()

    def test_scheduled_workflow_cannot_mix_revisions(self):
        from fastapi import HTTPException
        from keep.workflowmanager.workflowstore import WorkflowStore
        self.native_resources()
        self.apply()
        owner = next(item for item in self.service.status()["resources"] if item["kind"] == "workflows")
        with Session(self.engine) as session:
            revision = session.get(Workflow, owner["target_id"]).revision
        document = yaml.safe_load((self.directory / "workflows.yaml").read_text())
        document["workflow"]["actions"][0]["provider"]["with"]["value"] = "changed"
        self.bundle["workflows"][0]["artifact"] = self.artifact("workflows.yaml", document)
        self.apply()
        with self.assertRaises(HTTPException) as failure:
            WorkflowStore().get_workflow("tenant", owner["target_id"], expected_revision=revision)
        self.assertEqual(failure.exception.status_code, 409)

    def test_current_workflow_python_conditions_are_preserved(self):
        self.native_resources()
        document = yaml.safe_load((self.directory / "workflows.yaml").read_text())
        document["workflow"]["actions"][0]["if"] = "1 > 0 and True"
        self.bundle["workflows"][0]["artifact"] = self.artifact("workflows.yaml", document)
        self.apply()
        document["workflow"]["actions"][0]["if"] = "broken ("
        self.bundle["workflows"][0]["artifact"] = self.artifact("workflows.yaml", document)
        with self.assertRaisesRegex(ValueError, "invalid workflow expression"):
            self.candidate()


class IncidentProvisioningApiTest(IncidentProvisioningCase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from keep.api.routes import incident_policies
        from keep.identitymanager.authenticatedentity import AuthenticatedEntity
        self.routes = incident_policies
        self.entity = AuthenticatedEntity(tenant_id="tenant", email="admin@example.test", role="admin")
        app = FastAPI()
        self.app = app
        app.include_router(incident_policies.router, prefix="/settings/incident-policies")
        dependency = incident_policies.administrator.__defaults__[0].dependency
        app.dependency_overrides[dependency] = lambda: self.entity
        write_dependency = incident_policies.configuration_writer.__defaults__[0].dependency
        app.dependency_overrides[write_dependency] = lambda: self.entity
        self.client = self.enterContext(TestClient(app))

    def payload(self):
        return {"bundle": self.bundle, "artifacts": {path.name: path.read_text() for path in self.directory.iterdir()}}

    def test_api_preview_apply_and_conflict(self):
        prefix = "/settings/incident-policies"
        request = self.payload()
        self.assertEqual(self.client.post(prefix + "/validate", json=request).status_code, 200)
        preview = self.client.post(prefix + "/preview", json=request).json()
        body = {**request, "expected_active_digest": preview["active_digest"],
                "expected_candidate_digest": preview["candidate_digest"], "expected_preview_digest": preview["preview_digest"]}
        self.assertEqual(self.client.post(prefix + "/apply", json=body).status_code, 200)
        self.assertEqual(self.client.post(prefix + "/apply", json=body).status_code, 409)
        self.assertEqual(self.client.get(prefix).json()["generation"], 1)

    def test_api_restore_requires_explicit_deletions_and_fresh_preview(self):
        self.apply()
        self.bundle["mappings"].append({"id": "later", "artifact": self.artifact("later.yaml", {**self.mapping, "name": "later"})})
        self.apply()
        prefix = "/settings/incident-policies"
        self.assertEqual(self.client.get(prefix + "/versions/1/preview").status_code, 409)
        later = next(item for item in self.service.status()["resources"] if item["id"] == "later")
        request = {"generation": 1, "deletions": [{"kind": "mappings", "id": "later", "expected_resource_digest": later["digest"]}]}
        result = self.client.post(prefix + "/restore/preview", json=request)
        self.assertEqual(result.status_code, 200, result.text)
        preview = result.json()
        request.update(expected_active_digest=preview["active_digest"], expected_candidate_digest=preview["candidate_digest"],
                       expected_preview_digest=preview["preview_digest"])
        self.assertEqual(self.client.post(prefix + "/restore", json=request).status_code, 200)
        self.assertEqual(self.client.get(prefix).json()["generation"], 3)

    def test_responder_and_viewer_cannot_inspect_or_apply_global_config(self):
        for role in ("viewer", "responder"):
            self.entity.role = role
            self.assertEqual(self.client.get("/settings/incident-policies").status_code, 403)
            self.assertEqual(self.client.post("/settings/incident-policies/preview", json=self.payload()).status_code, 403)

    def test_read_only_blocks_apply_and_restore_before_any_write(self):
        prefix = "/settings/incident-policies"
        preview = self.client.post(prefix + "/preview", json=self.payload()).json()
        request = {**self.payload(), "expected_active_digest": preview["active_digest"],
                   "expected_candidate_digest": preview["candidate_digest"], "expected_preview_digest": preview["preview_digest"]}
        verifier = self.routes.configuration_writer.__defaults__[0].dependency
        self.app.dependency_overrides.pop(verifier)
        with patch.object(verifier, "read_only", True), patch.object(verifier, "read_only_bypass_keys", ["review-only"]):
            self.assertEqual(self.client.post(prefix + "/validate", json=self.payload()).status_code, 200)
            self.assertEqual(self.client.post(prefix + "/apply", json=request).status_code, 403)
            restore = {"generation": 1, "expected_active_digest": None,
                       "expected_candidate_digest": preview["candidate_digest"], "expected_preview_digest": preview["preview_digest"]}
            self.assertEqual(self.client.post(prefix + "/restore", json=restore).status_code, 403)
        self.assertIsNone(self.service.status()["active_digest"])

    def test_server_paths_and_foreign_tenant_are_not_accepted(self):
        payload = self.payload()
        payload["bundle"]["tenant_id"] = "foreign"
        self.assertEqual(self.client.post("/settings/incident-policies/validate", json=payload).status_code, 400)
        self.bundle["tenant_id"] = "tenant"
        self.bundle["access"]["path"] = "/etc/passwd"
        self.assertEqual(self.client.post("/settings/incident-policies/validate", json=self.payload()).status_code, 400)


if __name__ == "__main__":
    unittest.main()
