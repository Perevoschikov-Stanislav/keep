"""Policy examples, rejection cases and contract arithmetic; no provisioning."""

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from incident_contract import (
    CONTRACT_DIR, ROOT, SCHEMA, ContractError, parameter_inventory, read_yaml,
    artifact, validate_bundle, validate_policy, validate_shape, validate_workflow_shape,
)


EXAMPLES = CONTRACT_DIR / "examples"
FIXTURES = json.loads((CONTRACT_DIR / "fixtures-v1.json").read_text())
COMPATIBILITY = os.environ.get("KEEP_CONTRACT_COMPATIBILITY") == "1"


def example(name):
    return read_yaml(EXAMPLES / name / "bundle.yaml")


def check(bundle, name="a", **kwargs):
    return validate_bundle(bundle, EXAMPLES / name, "keep", **kwargs)


def at_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def field_at(event, path):
    value = event
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class IncidentContractTest(unittest.TestCase):
    def assert_rejected(self, bundle, location=None):
        with self.assertRaises(ContractError) as error:
            check(bundle)
        if location:
            self.assertIn(location, str(error.exception))

    def test_schema_examples_and_parameter_inventory(self):
        Draft202012Validator.check_schema(SCHEMA)
        digests = set()
        for name in ("a", "b"):
            with self.subTest(example=name):
                first = check(example(name), name, compatibility=COMPATIBILITY)
                self.assertEqual(first, check(example(name), name))
                digests.add(first["digest"])
        self.assertEqual(len(digests), 2)
        self.assertEqual((CONTRACT_DIR / "parameters-v1.md").read_text(), parameter_inventory())
        for name, node in SCHEMA["$defs"].items():
            if "properties" in node:
                self.assertIs(node["additionalProperties"], False, name)
                self.assertIn("x-change", node, name)

    def test_existing_team_policy_defaults_and_fixture_compatibility(self):
        policy = validate_policy({"version": 1})
        self.assertEqual(policy.visibility, "team")
        self.assertEqual(policy.teams, {})
        document = read_yaml(ROOT / "config/team-policy.example.yaml")
        policy = validate_policy(document)
        for team in document["teams"]:
            self.assertEqual(policy.teams_for_groups(set(team["groups"])), {team["id"]})
            self.assertEqual(policy.visible_teams({team["id"]}), {team["id"]})
        with self.assertRaises(ContractError):
            validate_policy({"version": 1.0})

    def test_no_runtime_loader_is_advertised(self):
        bundle = example("a")
        self.assertEqual(bundle["api_version"], "keep.incidents/v1")
        with self.assertRaises(ContractError):
            validate_policy(bundle)
        for key in ("KEEP_INCIDENT_POLICIES_FILE", "KEEP_INCIDENT_POLICIES"):
            self.assertNotIn(key, os.environ)

    def test_bundle_unknown_fields_and_security_switches_are_rejected(self):
        for key in ("unknown", "rbac_enabled", "team_isolation", "trust_actor", "allow_impersonation", "post_id", "channel_id"):
            bundle = example("a")
            bundle[key] = False
            self.assert_rejected(bundle, "bundle")
        for section in ("normalization", "correlation", "lifecycle", "automation", "routes", "transports",
                        "destinations", "proof_profiles", "service_clients", "subscribers"):
            bundle = example("a")
            bundle[section][0]["unknown"] = "ignored fields must fail"
            self.assert_rejected(bundle, section)
        bundle = example("a")
        bundle["routes"][0]["unknown"] = "private value"
        with self.assertRaises(ContractError) as error:
            check(bundle)
        self.assertIn("cedar-route", str(error.exception))
        self.assertIn("unknown", str(error.exception))
        self.assertNotIn("private value", str(error.exception))

    def test_every_resource_kind_rejects_duplicate_ids(self):
        for section in ("mappings", "workflows", "normalization", "presentations", "correlation", "lifecycle", "automation",
                        "contacts", "routes", "transports", "destinations", "proof_profiles", "service_clients", "subscribers"):
            bundle = example("a")
            bundle[section].append(copy.deepcopy(bundle[section][0]))
            self.assert_rejected(bundle, section)

    def test_dangling_references_and_unknown_teams_are_rejected(self):
        cases = [("correlation", "lifecycle_ref"), ("correlation", "presentation_ref"),
                 ("correlation", "automation_ref"), ("routes", "presentation_ref"),
                 ("destinations", "transport_ref")]
        for section, field in cases:
            bundle = example("a")
            bundle[section][0][field] = "absent"
            self.assert_rejected(bundle, section)
        for section, field in (("service_clients", "proof_profile_refs"), ("routes", "destination_refs"),
                               ("routes", "contact_refs"), ("subscribers", "destination_refs")):
            bundle = example("a")
            bundle[section][0][field] = ["absent"]
            self.assert_rejected(bundle, section)
        for section in ("normalization", "correlation", "automation", "routes", "service_clients", "subscribers"):
            bundle = example("a")
            bundle[section][0]["team_ids"] = ["absent"]
            self.assert_rejected(bundle, section)

    def test_wrong_trusted_tenant_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "trusted apply tenant"):
            validate_bundle(example("a"), EXAMPLES / "a", "other-tenant")

    def test_foreign_team_destination_and_contact_are_rejected(self):
        for field, value in (("destination_refs", ["quartz-json"]), ("contact_refs", ["quartz-oncall"])):
            bundle = example("a")
            bundle["routes"][0][field] = value
            self.assert_rejected(bundle, field)
        bundle = example("a")
        bundle["subscribers"][0]["destination_refs"] = ["quartz-json"]
        self.assert_rejected(bundle, "destination_refs")

    def test_ambiguous_priority_and_scope_are_rejected(self):
        for section in ("normalization", "correlation", "routes"):
            bundle = example("a")
            extra = copy.deepcopy(bundle[section][0])
            extra["id"] = "conflicting-rule"
            bundle[section].append(extra)
            self.assert_rejected(bundle, "priority")
        bundle = example("a")
        bundle["correlation"][0]["priority"] = bundle["correlation"][1]["priority"]
        self.assert_rejected(bundle, "priority")

    def test_unsupported_adapter_and_capability_changes_are_rejected(self):
        for field, value in (("kind", "unregistered"), ("adapter_ref", "unregistered"),
                             ("capabilities", {"update": True, "actions": False, "receipts": False})):
            bundle = example("a")
            bundle["transports"][0][field] = value
            self.assert_rejected(bundle, "transports")
        bundle = example("a")
        bundle["destinations"][0]["options"] = {"channel_id": "a-chat-channel"}
        self.assert_rejected(bundle, "destinations")
        bundle = example("a")
        del bundle["transports"][1]["callback_client_ref"]
        self.assert_rejected(bundle, "callback_client_ref")

    def test_noninteractive_transport_has_explicit_fallback(self):
        bundle = example("a")
        bundle["routes"][0]["update_fallback"] = "reject"
        self.assert_rejected(bundle, "update_fallback")
        bundle = example("a")
        bundle["routes"][0]["actions_fallback"] = "reject"
        self.assert_rejected(bundle, "actions_fallback")

    def test_mattermost_is_optional_and_domain_does_not_depend_on_transport(self):
        bundle = example("a")
        domain = {name: copy.deepcopy(bundle[name]) for name in ("access", "normalization", "correlation", "lifecycle", "automation")}
        bundle["transports"] = [item for item in bundle["transports"] if item["kind"] != "mattermost"]
        bundle["destinations"] = [item for item in bundle["destinations"] if item["transport_ref"] != "chat"]
        for contact in bundle["contacts"]:
            contact["addresses"] = [item for item in contact["addresses"] if item["transport_ref"] != "chat"]
        for route in bundle["routes"] + bundle["subscribers"]:
            route["destination_refs"] = [value for value in route["destination_refs"] if not value.endswith("-chat")]
        # SLA/levels/business state are unchanged; remove only the outgoing destination references.
        for automation in bundle["automation"]:
            for action in [*automation["levels"], automation["reminder"]]:
                action["destination_refs"] = [value for value in action["destination_refs"] if not value.endswith("-chat")]
        self.assertEqual(check(bundle)["resources"]["transports"], 1)
        for name in ("access", "normalization", "correlation", "lifecycle"):
            self.assertEqual(bundle[name], domain[name])
        self.assertEqual(bundle["automation"][0]["ack_deadline_seconds"], domain["automation"][0]["ack_deadline_seconds"])
        self.assertNotIn("mattermost", json.dumps(example("b")))

    def test_business_timer_bounds_and_retry_relationship_are_checked(self):
        for section, field in (("correlation", "window_seconds"), ("automation", "ack_deadline_seconds")):
            for value in (0, -1, 604801, True, "300"):
                bundle = example("a")
                bundle[section][0][field] = value
                self.assert_rejected(bundle, section)
        bundle = example("a")
        bundle["transports"][0]["delivery"]["retry"] = {"initial_backoff_seconds": 10, "max_backoff_seconds": 5}
        self.assert_rejected(bundle, "retry")

    def test_missing_fields_never_form_a_shared_key(self):
        bundle = example("a")
        bundle["correlation"][0]["required_fields"] = ["normalized.cluster"]
        self.assert_rejected(bundle, "required_fields")
        for field in ("tenant_id", "team_id", "role", "groups", "secrets.token"):
            bundle = example("a")
            bundle["correlation"][0]["group_by"] = [field]
            bundle["correlation"][0]["required_fields"] = [field]
            self.assert_rejected(bundle, "group_by")

    def test_canonical_security_fields_cannot_be_normalized(self):
        for field in ("team_id", "tenant_id", "role", "fingerprint"):
            bundle = example("a")
            bundle["normalization"][0]["fields"][0]["name"] = field
            self.assert_rejected(bundle, "normalization")
        bundle = example("a")
        bundle["normalization"][0]["fields"][0]["sources"] = ["labels.access_token"]
        self.assert_rejected(bundle, "sources")

    def test_regex_and_template_validation(self):
        bundle = example("a")
        bundle["normalization"][1]["fields"][-1]["extract"]["pattern"] = "["
        self.assert_rejected(bundle, "pattern")
        bundle = example("a")
        bundle["normalization"][1]["fields"][-1]["extract"]["group"] = 2
        self.assert_rejected(bundle, "group")
        for template in ("{{ secrets.api_key }}", "{{ normalized.resource", "{{ arbitrary() }}"):
            bundle = example("a")
            bundle["presentations"][0]["title"] = template
            self.assert_rejected(bundle, "title")
        bundle = example("a")
        bundle["presentations"][0]["links"] = [{"label": "Dashboard", "url_template": "https://metrics.example.org/d/cluster?orgId=1&var-workload={{ normalized.workload }}"}]
        check(bundle)
        bundle["presentations"][0]["links"][0]["url_template"] = "https://metrics.example.org/d/cluster?access_token=private-value"
        self.assert_rejected(bundle, "url_template")

    def test_removal_is_explicit_and_cannot_conflict_with_desired_resource(self):
        bundle = example("a")
        removal = {"kind": "correlation", "id": "workload", "expected_resource_digest": "0" * 64}
        bundle["deletions"] = [removal]
        self.assert_rejected(bundle, "deletions")
        removal["id"] = "previous-managed-rule"
        check(bundle)  # Matching the previous applied snapshot is a runtime preview/apply check in 24.
        bundle["deletions"].append(copy.deepcopy(removal))
        self.assert_rejected(bundle, "duplicate removal")

    def test_secrets_inline_and_endpoints_never_leak_in_errors(self):
        marker = "credential-that-must-never-appear-in-output"
        for value in (marker, "https://user:" + marker + "@example.org", "https://example.org/path?token=" + marker):
            bundle = example("a")
            bundle["transports"][0]["auth_ref"] = value
            with self.assertRaises(ContractError) as error:
                check(bundle)
            self.assertNotIn(marker, str(error.exception))
        for endpoint in ("https://example.org:70000", "https://example.org/a/../b", "https://user:password@example.org"):
            bundle = example("a")
            bundle["transports"][0]["endpoint"] = endpoint
            self.assert_rejected(bundle, "endpoint")

    def test_proof_profile_and_service_scope_cannot_disable_authorization(self):
        for field, value in (("algorithms", ["none"]), ("algorithms", ["HS256"]), ("max_age_seconds", 0),
                             ("required_claims", {}), ("trust_groups", True)):
            bundle = example("a")
            bundle["proof_profiles"][0][field] = value
            self.assert_rejected(bundle, "proof_profiles")
        for scopes in (["admin"], ["write:*"], ["delete:incident"]):
            bundle = example("a")
            bundle["service_clients"][0]["scopes"] = scopes
            self.assert_rejected(bundle, "scopes")
        bundle = example("a")
        client = copy.deepcopy(bundle["service_clients"][0])
        client["id"] = "other-client"
        client["origin"] = "other-origin"
        bundle["service_clients"].append(client)
        self.assert_rejected(bundle, "ambiguous service identity")

    def test_local_k3d_identity_is_supported_without_disabling_jwt_checks(self):
        bundle = example("a")
        profile = bundle["proof_profiles"][0]
        profile["issuer"] = "http://localhost:8180/realms/core"
        profile["jwks_url"] = "http://keycloak.keep-lab.svc.cluster.local:8080/realms/core/protocol/openid-connect/certs"
        check(bundle)
        profile["issuer"] = "http://public-identity.example.org/realms/keep"
        self.assert_rejected(bundle, "issuer")
        profile["issuer"] = "https://identity.example.org/realms/keep"
        profile["required_claims"] = {"exp": "never"}
        self.assert_rejected(bundle, "required_claims")

    def test_new_incident_resets_ack_and_sla(self):
        bundle = example("a")
        bundle["lifecycle"][0]["reopen"]["mode"] = "new_incident"
        bundle["lifecycle"][0]["reopen"]["ack"] = "preserve"
        self.assert_rejected(bundle, "ack")
        bundle["lifecycle"][0]["reopen"]["ack"] = "reset"
        bundle["automation"][0]["on_reopen"] = "continue"
        self.assert_rejected(bundle, "automation_ref")

    def test_wire_messages_and_incident_commands_reject_actor_spoofing(self):
        for message in FIXTURES["messages"]:
            validate_shape(message["schema"], message["body"], message["schema"])
        command = next(item["body"] for item in FIXTURES["messages"] if item["schema"] == "IncidentCommand")
        for field in ("actor", "actor_token", "role", "groups", "origin", "tenant_id", "team_id", "channel_id", "post_id"):
            value = {**command, field: "spoofed"}
            with self.assertRaises(ContractError):
                validate_shape("IncidentCommand", value, "command")
        value = {**command, "command": "assign", "assignee": "local-user-id"}
        validate_shape("IncidentCommand", value, "command")
        del value["assignee"]
        with self.assertRaises(ContractError):
            validate_shape("IncidentCommand", value, "command")

    def test_notification_requires_no_transport_specific_identity(self):
        notification = next(item["body"] for item in FIXTURES["messages"] if item["schema"] == "Notification")
        for transport in ("json", "chat"):
            value = {**notification, "transport_ref": transport}
            validate_shape("Notification", value, "notification")
        for field in ("attachments", "post_id", "channel_id", "mm_flaps", "mm_members", "snooze_until"):
            with self.assertRaises(ContractError):
                validate_shape("Notification", {**notification, field: []}, "notification")
        value = copy.deepcopy(notification)
        value["actions"][0]["keep_url"] = "https://keep.example.org/incidents/id?actor_token=private"
        with self.assertRaises(ContractError):
            validate_shape("Notification", value, "notification")

    def test_atomic_input_integrity_missing_mount_and_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="contract-input-") as directory:
            target = Path(directory) / "bundle"
            shutil.copytree(EXAMPLES / "a", target)
            bundle = read_yaml(target / "bundle.yaml")
            first = validate_bundle(bundle, target, "keep")
            path = target / bundle["access"]["path"]
            original = path.read_bytes()
            path.write_bytes(original + b"\n# changed mount\n")
            with self.assertRaisesRegex(ContractError, "digest mismatch"):
                validate_bundle(bundle, target, "keep")
            path.write_bytes(original)
            self.assertEqual(first, validate_bundle(bundle, target, "keep"))
            path.unlink()
            with self.assertRaisesRegex(ContractError, "unavailable"):
                validate_bundle(bundle, target, "keep")
            external = Path(directory) / "external.yaml"
            external.write_bytes(original)
            path.symlink_to(external)
            with self.assertRaisesRegex(ContractError, "escapes bundle"):
                validate_bundle(bundle, target, "keep")
        # Validation outcomes do not replace the runtime provisioning retention checks.

    def test_modified_mapping_cannot_write_security_fields_or_unknown_zone(self):
        with tempfile.TemporaryDirectory(prefix="contract-mapping-") as directory:
            target = Path(directory)
            shutil.copytree(EXAMPLES / "a", target, dirs_exist_ok=True)
            baseline = read_yaml(target / "bundle.yaml")
            path = target / baseline["mappings"][0]["artifact"]["path"]
            original = read_yaml(path)
            for field, value in (("team_id", "cedar"), ("tenant_id", "other"), ("role", "admin"),
                                 ("zone", "unregistered"), ("access_token", "literal")):
                modified = copy.deepcopy(original)
                modified["rows"][0][field] = value
                path.write_text(yaml.safe_dump(modified))
                bundle = copy.deepcopy(baseline)
                bundle["mappings"][0]["artifact"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ContractError):
                    validate_bundle(bundle, target, "keep")

    def test_artifact_parsing_uses_the_verified_bytes(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="contract-snapshot-") as directory:
            target = Path(directory)
            path = target / "access.yaml"
            payload = b"version: 1\n"
            path.write_bytes(payload)
            document = {"path": "access.yaml", "sha256": hashlib.sha256(payload).hexdigest()}
            with patch.object(Path, "read_text", side_effect=AssertionError("must not reopen the verified file")):
                self.assertEqual(artifact(target, document, "access"), {"version": 1})

    def test_duplicate_yaml_and_cycles_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="contract-yaml-") as directory:
            path = Path(directory) / "input.yaml"
            for content in ("version: 1\nversion: 2\n", "a: &x [*x]\n", "a: .inf\n", "a: 2026-10-04\n"):
                path.write_text(content)
                with self.assertRaises(ContractError):
                    read_yaml(path)

    def test_workflow_uses_existing_syntax_and_reference_only_credentials(self):
        document = read_yaml(EXAMPLES / "a/workflows/record-evidence.yaml")
        for field in ("unknown", "trust_actor", "admin"):
            value = copy.deepcopy(document)
            value["workflow"][field] = True
            with self.assertRaises(ContractError):
                validate_workflow_shape(value, "workflow")
        value = copy.deepcopy(document)
        parameters = value["workflow"]["actions"][0]["provider"]["with"]
        parameters["headers"] = {"X-Bridge-Secret": "inline-credential"}
        with self.assertRaises(ContractError):
            validate_workflow_shape(value, "workflow")
        parameters["headers"] = {"Authorization": "Bearer {{ secrets.integration }}"}
        validate_workflow_shape(value, "workflow")

    def test_fixture_arithmetic_explains_two_policy_variants(self):
        self.assertEqual(FIXTURES["schema_version"], 1)
        ids = set()
        for scenario in FIXTURES["scenarios"]:
            self.assertNotIn(scenario["id"], ids)
            ids.add(scenario["id"])
            for name in ("a", "b"):
                bundle = example(name)
                rule = next((item for item in bundle["correlation"] if item["id"] == scenario.get("rule")), None)
                lifecycle, automation = bundle["lifecycle"][0], bundle["automation"][0]
                expected = scenario["expected"][name]
                with self.subTest(scenario=scenario["id"], example=name):
                    if scenario["id"] == "replicas":
                        keys = [tuple(field_at(item, path) for path in rule["group_by"]) for item in scenario["inputs"]]
                        self.assertEqual(keys[0] == keys[1], expected["same_group"])
                    elif scenario["id"] == "join-window":
                        self.assertEqual(scenario["elapsed_seconds"] < rule["window_seconds"], expected["within_window"])
                    elif scenario["id"] == "reopen":
                        elapsed = (at_time(scenario["recurrence_at"]) - at_time(scenario["resolved_at"])).total_seconds()
                        self.assertEqual(lifecycle["reopen"]["mode"] == "reopen" and elapsed < lifecycle["reopen"]["within_seconds"], expected["same_id"])
                        self.assertEqual(lifecycle["reopen"]["ack"], expected["ack"])
                    elif scenario["id"] == "ack-deadline":
                        deadline = at_time(scenario["created_at"]) + timedelta(seconds=automation["ack_deadline_seconds"])
                        self.assertEqual(deadline, at_time(expected["deadline_at"]))
                    elif scenario["id"] == "flapping":
                        self.assertEqual(scenario["transitions_in_window"] >= lifecycle["flapping"]["transition_threshold"], expected["flapping"])
                    elif scenario["id"] == "delivery":
                        team = read_yaml(EXAMPLES / name / "team-policy.yaml")["teams"][scenario["team_index"]]["id"]
                        selected = [route for route in bundle["routes"] if team in route["team_ids"] and scenario["event_type"] in route["event_types"]]
                        destinations = {item for route in selected for item in route["destination_refs"]}
                        transports = {item["transport_ref"] for item in bundle["destinations"] if item["id"] in destinations}
                        kinds = sorted(item["kind"] for item in bundle["transports"] if item["id"] in transports)
                        self.assertEqual(kinds, expected["kinds"])
                        self.assertTrue(expected["independent"])
                    elif scenario["id"] == "missing-required":
                        self.assertTrue(any(field_at(scenario["input"], path) is None for path in rule["required_fields"]))
                        self.assertEqual(rule["missing_required"], expected["decision"])
                    elif scenario["id"] == "foreign-team":
                        self.assertEqual([dest for route in bundle["routes"] if scenario["team_id"] in route["team_ids"]
                                          for dest in route["destination_refs"]], expected["destinations"])
                    elif scenario["id"] == "late-event":
                        self.assertLess(at_time(scenario["event_time"]), at_time(scenario["last_transition_at"]))
                        self.assertEqual(lifecycle["late_event_policy"], expected["decision"])
                    elif scenario["id"] == "tenant-team-boundary":
                        self.assertFalse(expected["different_team_same_group"])
                        self.assertFalse(expected["different_tenant_same_group"])
                        self.assertIn("tenant/team/rule", SCHEMA["$defs"]["Correlation"]["properties"]["group_by"]["description"])
                    else:
                        self.fail("Scenario needs a documented contract assertion")
        self.assertEqual(len(ids), 10)

    def test_normalization_fixtures_define_labels_aliases_and_regex_precedence(self):
        self.assertEqual(len(FIXTURES["normalization_events"]), 10)
        for fixture in FIXTURES["normalization_events"]:
            for name in ("a", "b"):
                bundle = example(name)
                rule = next(item for item in bundle["normalization"] if item["id"] == fixture["normalization_ref"])
                values = {}
                for field in rule["fields"]:
                    value = next((field_at(fixture["event"], path) for path in field["sources"]
                                  if field_at(fixture["event"], path) not in (None, "")), None)
                    if value is not None and "extract" in field:
                        match = re.search(field["extract"]["pattern"], value)
                        value = match.group(field["extract"].get("group", 1)) if match else None
                    if value is None and field["missing"]["mode"] == "literal":
                        value = field["missing"]["value"]
                    values[field["name"]] = value
                with self.subTest(event=fixture["id"], example=name):
                    for field, expected in fixture["expected"][name].items():
                        self.assertEqual(values[field], expected)
                    if COMPATIBILITY:
                        import celpy
                        environment = celpy.Environment()
                        activation = celpy.json_to_cel(fixture["event"])
                        matches = [candidate for candidate in bundle["normalization"]
                                   if environment.program(environment.compile(candidate["match"])).evaluate(activation)]
                        self.assertTrue(matches)
                        self.assertEqual(max(matches, key=lambda item: item["priority"])["id"], rule["id"])

    @unittest.skipUnless(COMPATIBILITY, "Current Keep dependencies are checked in k3d")
    def test_actual_cel_compiler_rejects_invalid_policy(self):
        bundle = example("a")
        bundle["correlation"][0]["match"] = "invalid ((("
        with self.assertRaisesRegex(ContractError, "CEL compiler"):
            check(bundle, compatibility=True)

    @unittest.skipUnless(COMPATIBILITY, "Current Keep dependencies are checked in k3d")
    def test_correlation_match_handles_missing_null_empty_and_wrong_type(self):
        import celpy

        environment = celpy.Environment()
        for name in ("a", "b"):
            for rule in example(name)["correlation"]:
                program = environment.program(environment.compile(rule["match"]))
                for normalized in ({}, {"kind": None}, {"kind": ""}, {"kind": 12}):
                    with self.subTest(example=name, rule=rule["id"], normalized=normalized):
                        self.assertFalse(program.evaluate(celpy.json_to_cel({"normalized": normalized})))


if __name__ == "__main__":
    # Both host and k3d runner provide this persistent/explicit path; never use system /tmp.
    temporary = Path(os.environ.get("TMPDIR", ROOT / ".lab-work/incident-contract/tmp"))
    temporary.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary)
    unittest.main(verbosity=2)
