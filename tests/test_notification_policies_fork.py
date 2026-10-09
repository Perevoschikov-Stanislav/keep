"""Notification policies must change behavior through validated configuration."""

import copy
import unittest

from keep.api.core.incident_contract import ContractError, validate_shape


class NotificationPoliciesTest(unittest.TestCase):
    def bundle(self):
        return {
            "notification_defaults": {"events": {
                "incident.created": {"do": ["post"]},
                "alert.added": {"do": ["edit", "thread"], "throttle_seconds": 300},
            }},
            "notification_policies": [
                {"id": "team", "scope": "team", "team_id": "alpha",
                 "settings": {"events": {"alert.added": {"throttle_seconds": 60}}}},
                {"id": "route", "scope": "route", "team_id": "alpha", "route_ref": "engineering",
                 "settings": {"events": {"alert.added": {"do": ["thread"]}}}},
                {"id": "family", "scope": "family", "team_id": "alpha", "route_ref": "engineering",
                 "family_ref": "storage", "settings": {"events": {"alert.added": {"do": ["edit"]}}}},
                {"id": "level", "scope": "level", "team_id": "alpha", "route_ref": "engineering",
                 "family_ref": "storage", "level_id": "urgent",
                 "settings": {"events": {"alert.added": {"throttle_seconds": 5}}}},
            ],
        }

    def effective(self, bundle=None, **fields):
        from keep.api.core.notification_policies import effective_policy
        return effective_policy(bundle or self.bundle(), team_id=fields.pop("team_id", "alpha"),
                                route_id=fields.pop("route_id", "engineering"), **fields)

    def test_most_specific_scope_wins_and_partial_dictionary_retains_defaults(self):
        result = self.effective(family_id="storage", level_id="urgent")
        self.assertEqual(result["settings"]["events"]["alert.added"],
                         {"do": ["edit"], "throttle_seconds": 5})
        self.assertEqual(result["settings"]["events"]["incident.created"], {"do": ["post"]})
        self.assertEqual(result["sources"]["events.alert.added.do"], "family")
        self.assertEqual(result["sources"]["events.alert.added.throttle_seconds"], "level")

    def test_lists_are_replaced_without_combining_delivery_actions(self):
        result = self.effective()
        self.assertEqual(result["settings"]["events"]["alert.added"]["do"], ["thread"])

    def test_unrelated_team_route_family_and_level_do_not_inherit_overrides(self):
        cases = [({"team_id": "beta"}, ["edit", "thread"], 300),
                 ({"route_id": "other"}, ["edit", "thread"], 60),
                 ({"family_id": "compute", "level_id": "urgent"}, ["thread"], 60),
                 ({"family_id": "storage", "level_id": "normal"}, ["edit"], 60)]
        for fields, actions, throttle in cases:
            with self.subTest(fields=fields):
                value = self.effective(**fields)["settings"]["events"]["alert.added"]
                self.assertEqual(value, {"do": actions, "throttle_seconds": throttle})

    def test_empty_action_list_can_disable_inherited_buttons(self):
        bundle = self.bundle()
        bundle["notification_defaults"]["buttons"] = [{"command": "ack", "label": "Take"}]
        bundle["notification_policies"][0]["settings"]["buttons"] = []
        self.assertEqual(self.effective(bundle)["settings"]["buttons"], [])

    def test_resolution_does_not_mutate_published_configuration(self):
        bundle = self.bundle()
        before = copy.deepcopy(bundle)
        result = self.effective(bundle)
        result["settings"]["events"]["incident.created"]["do"].append("thread")
        self.assertEqual(bundle, before)

    def test_inheritance_is_independent_of_yaml_array_order(self):
        bundle = self.bundle()
        expected = self.effective(bundle, family_id="storage", level_id="urgent")
        bundle["notification_policies"].reverse()
        self.assertEqual(self.effective(bundle, family_id="storage", level_id="urgent"), expected)

    def test_duplicate_scope_is_rejected_instead_of_using_array_order(self):
        bundle = self.bundle()
        duplicate = copy.deepcopy(bundle["notification_policies"][0])
        duplicate["id"] = "other-team-rule"
        bundle["notification_policies"].append(duplicate)
        with self.assertRaisesRegex(ContractError, "ambiguous notification scope"):
            self.effective(bundle)

    def test_settings_schema_accepts_event_policy_and_explicit_fallback(self):
        validate_shape("NotificationSettings", {
            "events": {"incident.reopened": {"do": ["edit", "thread"],
                "within_seconds": 3600, "else_do": ["post"], "when": 'incident.status == "firing"'}},
            "fallbacks": {"thread": "post"},
        }, "settings")

    def test_unknown_events_actions_and_mixed_none_are_rejected(self):
        cases = [{"events": {"incident.unregistered": {"do": ["post"]}}},
                 {"events": {"incident.created": {"do": ["execute_python"]}}},
                 {"events": {"incident.created": {"do": ["none", "post"]}}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_shape("NotificationSettings", value, "settings")

    def test_policy_requires_complete_parent_scope_and_forbids_extra_selectors(self):
        for value in [
            {"id": "bad", "scope": "family", "family_ref": "storage", "settings": {}},
            {"id": "bad", "scope": "team", "team_id": "alpha", "route_ref": "engineering", "settings": {}},
        ]:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_shape("NotificationPolicy", value, "policy")

    def test_old_configuration_has_no_implicit_notification_policy(self):
        from keep.api.core.notification_policies import effective_policy
        self.assertEqual(effective_policy({}, team_id="alpha", route_id="engineering"),
                         {"settings": {}, "sources": {}})

    def transport_bundle(self, actions):
        return {"routes": [{"id": "engineering", "team_ids": ["alpha"],
                            "destination_refs": ["target"], "presentation_ref": "card"}],
                "transports": [{"id": "http", "capabilities": {
                    "update": False, "thread": False, "actions": False, "receipts": False}}],
                "destinations": [{"id": "target", "team_id": "alpha", "transport_ref": "http"}],
                "presentations": [{"id": "card", "title": "{{ incident.name }}",
                                   "description": "{{ incident.status }}"}],
                "notification_defaults": {"events": {"incident.created": {"do": actions}}}}

    def test_unsupported_thread_rejects_bundle_without_explicit_fallback(self):
        from keep.api.core.notification_policies import validate_policies
        with self.assertRaisesRegex(ContractError, "thread.*explicit fallback"):
            validate_policies(self.transport_bundle(["thread"]), {"alpha"})

    def test_fallback_is_checked_after_inheritance(self):
        from keep.api.core.notification_policies import validate_policies
        bundle = self.transport_bundle(["edit", "thread"])
        bundle["notification_defaults"]["fallbacks"] = {"edit": "post", "thread": "none"}
        validate_policies(bundle, {"alpha"})
        bundle["notification_policies"] = [{"id": "override", "scope": "team", "team_id": "alpha",
            "settings": {"events": {"incident.created": {"do": ["repost"]}}}}]
        with self.assertRaisesRegex(ContractError, "repost.*explicit fallback"):
            validate_policies(bundle, {"alpha"})

    def test_policy_references_require_matching_ownership(self):
        from keep.api.core.notification_policies import validate_policies
        bundle = self.transport_bundle(["post"])
        for fields in [{"team_id": "missing"}, {"team_id": "beta"}]:
            bundle["notification_policies"] = [{"id": "invalid", "scope": "route", "route_ref": "engineering",
                                               "settings": {}, **fields}]
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                validate_policies(bundle, {"alpha", "beta"})

    def test_preview_shows_effective_settings_sources_and_fallback(self):
        from keep.api.core.notification_policies import preview_notifications
        bundle = self.transport_bundle(["thread"])
        bundle["notification_defaults"]["fallbacks"] = {"thread": "post"}
        bundle["notification_defaults"]["lines"] = {"incident.created": "Created: {{ incident.name }}"}
        result = preview_notifications(bundle)
        self.assertEqual(result[0]["actions"], ["post"])
        self.assertEqual(result[0]["fallbacks"], [{"from": "thread", "to": "post"}])
        self.assertIn("Created:", result[0]["line"])
        self.assertEqual(result[0]["sources"]["events.incident.created.do"], "notification_defaults")

    def test_preview_cannot_expand_secrets_or_raw_events(self):
        from keep.api.core.notification_policies import validate_policies
        for template in ["{{ secrets.key }}", "{{ incident.raw_event.password }}", "{{ raw.labels }}"]:
            bundle = self.transport_bundle(["post"])
            bundle["notification_defaults"]["lines"] = {"incident.created": template}
            with self.subTest(template=template), self.assertRaises(ContractError):
                validate_policies(bundle, {"alpha"})
