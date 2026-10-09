"""Validate the v1 wire examples; this does not execute the silence service."""

import copy
import json
import sys
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


CONTRACT_DIR = Path(__file__).resolve().parents[1] / "docs/fork/silences"
SCHEMA = json.loads((CONTRACT_DIR / "contract-v1.schema.json").read_text())
FIXTURES = json.loads((CONTRACT_DIR / "fixtures-v1.json").read_text())
MESSAGES = FIXTURES["messages"]
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def valid_datetime(value):
    if not isinstance(value, str):
        return True
    return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def validator(message_type):
    return Draft202012Validator(
        {
            "$schema": SCHEMA["$schema"],
            "$defs": SCHEMA["$defs"],
            "$ref": f"#/$defs/{message_type}",
        },
        format_checker=FORMAT_CHECKER,
    )


def body(name):
    return copy.deepcopy(MESSAGES[name]["body"])


def time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SilenceContractTest(unittest.TestCase):
    def assert_invalid(self, message_type, value):
        self.assertFalse(validator(message_type).is_valid(value))

    def test_schema_and_all_wire_examples(self):
        Draft202012Validator.check_schema(SCHEMA)
        root = Draft202012Validator(SCHEMA, format_checker=FORMAT_CHECKER)
        for name, message in MESSAGES.items():
            with self.subTest(message=name):
                message_validator = validator(message["schema"])
                errors = list(message_validator.iter_errors(message["body"]))
                self.assertFalse(errors, "\n".join(str(error) for error in errors))
                self.assertTrue(root.is_valid(message["body"]))
        self.assertEqual(
            {message["schema"] for message in MESSAGES.values()},
            {"CreateCommand", "UpdateCommand", "CancelCommand", "Silence",
             "MutationResponse", "SilenceList", "EffectiveQuery",
             "EffectiveResponse", "LifecycleEvent", "Error"},
        )

    def test_commands_reject_server_owned_fields(self):
        for name in ("create-alert", "extend-alert", "cancel-alert"):
            for key in ("actor", "created_by", "updated_by", "tenant_id", "role",
                        "groups", "origin", "revision", "actor_token", "unknown"):
                with self.subTest(message=name, field=key):
                    value = body(name)
                    value[key] = "not trusted"
                    self.assert_invalid(MESSAGES[name]["schema"], value)
        for key in ("team_id", "tenant_id", "created_by", "origin"):
            value = body("extend-alert")
            value["changes"][key] = "not trusted"
            self.assert_invalid("UpdateCommand", value)

    def test_request_ids_and_expected_revision_are_required(self):
        for name in ("create-alert", "extend-alert", "cancel-alert"):
            value = body(name)
            del value["client_request_id"]
            self.assert_invalid(MESSAGES[name]["schema"], value)
        for name in ("extend-alert", "cancel-alert"):
            for invalid_revision in (None, 0, -1, True, "1"):
                value = body(name)
                value["expected_revision"] = invalid_revision
                self.assert_invalid(MESSAGES[name]["schema"], value)
            value = body(name)
            del value["expected_revision"]
            self.assert_invalid(MESSAGES[name]["schema"], value)
        value = body("extend-alert")
        value["changes"] = {}
        self.assert_invalid("UpdateCommand", value)

    def test_selector_is_unambiguous_and_bounded(self):
        fingerprint = body("create-alert")["selector"]["fingerprints"][0]
        invalid_selectors = [
            {"kind": "alert", "fingerprints": []},
            {"kind": "alert", "fingerprints": [fingerprint, fingerprint]},
            {"kind": "alert", "fingerprints": [str(n) for n in range(1001)]},
            {"kind": "alert", "fingerprints": [fingerprint], "cel": "true"},
            {"kind": "filter", "cel": ""},
            {"kind": "filter", "cel": "x" * 8193},
            {"kind": "incident", "incident_ids": ["not-a-uuid"]},
            {"kind": "unknown", "fingerprints": [fingerprint]},
        ]
        for selector in invalid_selectors:
            with self.subTest(selector_kind=selector.get("kind")):
                value = body("create-alert")
                value["selector"] = selector
                self.assert_invalid("CreateCommand", value)

    def test_only_utc_timestamps_and_known_versions(self):
        for timestamp in ("2026-10-04T11:00:00", "2026-10-04T11:00:00+00:00",
                          "2026-10-04T11:00:00.1234567Z", "2026-02-30T11:00:00Z"):
            value = body("create-alert")
            value["ends_at"] = timestamp
            self.assert_invalid("CreateCommand", value)
        for version in (None, True, "1", 0, 2):
            for name in ("create-alert", "rule-single", "event-created"):
                value = body(name)
                value["schema_version"] = version
                self.assert_invalid(MESSAGES[name]["schema"], value)

    def test_effective_shapes_and_counts(self):
        for message in MESSAGES.values():
            if message["schema"] != "EffectiveResponse":
                continue
            for item in message["body"]["items"]:
                self.assertLessEqual(item["silenced_alerts"], item["total_alerts"])
                if item["coverage"] == "partial":
                    self.assertLess(item["silenced_alerts"], item["total_alerts"])
                if item["coverage"] == "full":
                    self.assertEqual(item["silenced_alerts"], item["total_alerts"])
                for reason in item["reasons"]:
                    if reason["ends_at"] is not None:
                        self.assertGreater(time(reason["ends_at"]), time(message["body"]["evaluated_at"]))
        value = body("effective-partial")
        value["items"][1]["silenced"] = True
        self.assert_invalid("EffectiveResponse", value)
        value = body("effective-at-end")
        value["items"][0]["coverage"] = "full"
        self.assert_invalid("EffectiveResponse", value)
        value = body("effective-query")
        value["targets"].append(copy.deepcopy(value["targets"][0]))
        self.assert_invalid("EffectiveQuery", value)

    def test_resource_snapshots_agree_with_their_clocks(self):
        resources = []
        for message in MESSAGES.values():
            value = message["body"]
            if message["schema"] == "Silence":
                resources.append(value)
            elif message["schema"] == "MutationResponse":
                resources.append(value["result"])
            elif message["schema"] == "LifecycleEvent":
                resources.append(value["resource"])
            elif message["schema"] == "SilenceList":
                resources.extend(value["items"])
                for item in value["items"]:
                    self.assertEqual(item["evaluated_at"], value["evaluated_at"])
        for resource in resources:
            with self.subTest(id=resource["id"], revision=resource["revision"]):
                at = time(resource["evaluated_at"])
                self.assertLessEqual(time(resource["created_at"]), time(resource["updated_at"]))
                self.assertLessEqual(time(resource["updated_at"]), at)
                if resource["ends_at"] is not None:
                    self.assertGreater(time(resource["ends_at"]), time(resource["starts_at"]))
                if resource["cancelled_at"] is not None:
                    self.assertLessEqual(time(resource["cancelled_at"]), at)
                    self.assertEqual(resource["state"], "cancelled")
                elif at < time(resource["starts_at"]):
                    self.assertEqual(resource["state"], "scheduled")
                elif resource["ends_at"] is not None and at >= time(resource["ends_at"]):
                    self.assertEqual(resource["state"], "expired")
                else:
                    self.assertEqual(resource["state"], "active")

    def test_lifecycle_matches_resource_and_revision_order(self):
        ids = set()
        revisions = defaultdict(list)
        for message in MESSAGES.values():
            if message["schema"] != "LifecycleEvent":
                continue
            event = message["body"]
            resource = event["resource"]
            self.assertNotIn(event["event_id"], ids)
            ids.add(event["event_id"])
            revisions[event["silence_id"]].append(event["revision"])
            for field in ("revision", "tenant_id", "team_id"):
                self.assertEqual(event[field], resource[field])
            self.assertEqual(event["silence_id"], resource["id"])
            self.assertEqual(event["actor"], resource["updated_by"])
            self.assertLessEqual(time(event["effective_at"]), time(event["occurred_at"]))
            event_type = event["event_type"]
            if event_type == "silence.activated":
                self.assertEqual(event["effective_at"], resource["starts_at"])
                self.assertEqual(resource["state"], "active")
            elif event_type == "silence.expired":
                self.assertEqual(event["effective_at"], resource["ends_at"])
                self.assertEqual(resource["state"], "expired")
            elif event_type == "silence.cancelled":
                self.assertEqual(event["effective_at"], resource["cancelled_at"])
                self.assertEqual(resource["state"], "cancelled")
        for values in revisions.values():
            self.assertEqual(sorted(values), list(range(min(values), max(values) + 1)))

    def test_replay_keeps_original_snapshot_and_concurrent_edit_has_new_key(self):
        first, replay = body("create-result"), body("create-replay")
        self.assertEqual(first["result"], replay["result"])
        self.assertEqual(first["client_request_id"], replay["client_request_id"])
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(MESSAGES["create-result"]["http_status"], MESSAGES["create-replay"]["http_status"])
        self.assertNotEqual(body("extend-alert")["client_request_id"], body("update-stale")["client_request_id"])

    def test_response_extensions_are_allowed_but_request_extensions_are_not(self):
        for name in ("rule-single", "event-created", "effective-partial"):
            value = body(name)
            value["future_optional_field"] = "ignored by receivers"
            self.assertTrue(validator(MESSAGES[name]["schema"]).is_valid(value))
        value = body("create-alert")
        value["future_optional_field"] = "not accepted by the server"
        self.assert_invalid("CreateCommand", value)

    def test_scenario_references_and_required_edge_cases(self):
        self.assertEqual(FIXTURES["schema_version"], 1)
        scenario_ids = set()
        for scenario in FIXTURES["scenarios"]:
            self.assertNotIn(scenario["id"], scenario_ids)
            scenario_ids.add(scenario["id"])
            self.assertIsInstance(scenario["given"], dict)
            self.assertTrue(scenario["expected"])
            for reference in scenario["message_refs"]:
                self.assertIn(reference, MESSAGES)
        self.assertTrue({"utc-half-open-boundaries", "incoming-fingerprint-and-resolved",
                         "overlap-cancel-does-not-restore", "partial-incident-safe-content",
                         "shared-alert-no-recursive-propagation", "mm-group-snapshot-not-dynamic",
                         "atomic-foreign-group-hidden", "command-retry-after-lost-response",
                         "concurrent-update", "no-actor-proof-no-mm-command",
                         "verified-operator-mm-command", "delayed-worker-does-not-extend",
                         "receiver-duplicate-and-reorder"} <= scenario_ids)

    def test_wire_examples_do_not_contain_credentials(self):
        forbidden = {"access_token", "refresh_token", "actor_token", "actor_proof",
                     "authorization", "x-api-key", "x-keep-actor-token", "password", "secret"}

        def check(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden & {key.lower() for key in value})
                for nested in value.values():
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)

        for message in MESSAGES.values():
            check(message["body"])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SilenceContractTest)
    )
    if result.wasSuccessful():
        print(f"Validated {len(MESSAGES)} wire examples and {len(FIXTURES['scenarios'])} scenario definitions. Silence runtime was not executed.")
    sys.exit(0 if result.wasSuccessful() else 1)
