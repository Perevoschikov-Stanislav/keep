"""Policy preview and publication exercise the existing atomic IaC path."""

import copy
from sqlmodel import Session

from keep.api.core.incident_contract import ContractError
from keep.api.models.db.incident_configuration import IncidentConfiguration
from tests.test_incident_notifications_fork import NotificationCase


class NotificationPolicyRuntimeTest(NotificationCase):
    def policy(self):
        self.bundle["notification_defaults"] = {
            "events": {"incident.created": {"do": ["post"]},
                       "incident.acknowledged": {"do": ["edit", "thread"]}},
            "fallbacks": {"edit": "post", "thread": "post"},
            "lines": {"incident.acknowledged": "Taken by {{ event.actor }}"},
        }
        self.bundle["notification_policies"] = [{"id": "alpha", "scope": "team", "team_id": "alpha",
            "settings": {"events": {"incident.acknowledged": {"throttle_seconds": 30}}}}]

    def test_preview_includes_inherited_configuration_and_each_event_before_apply(self):
        self.policy()
        before = self.service.status()
        preview = self.service.preview(self.candidate())
        self.assertEqual(before, self.service.status())
        sample = next(item for item in preview["notification_preview"] if item["team_id"] == "alpha"
                      and item["event_type"] == "incident.acknowledged" and item["destination_ref"] == "alpha-http")
        self.assertEqual(sample["actions"], ["post"])
        self.assertEqual(sample["settings"]["events"]["incident.acknowledged"]["throttle_seconds"], 30)
        self.assertEqual(sample["sources"]["events.incident.acknowledged.throttle_seconds"], "alpha")
        self.assertIn("Taken by", sample["line"])

    def test_repeated_apply_publishes_one_managed_policy_and_no_notifications(self):
        self.policy()
        first = self.apply()
        self.assertEqual(self.apply()["generation"], first["generation"])
        with Session(self.engine) as session:
            snapshot = session.get(IncidentConfiguration, "tenant").snapshot
            self.assertEqual(len([item for item in snapshot["resources"] if item["kind"] == "notification_policies"]), 1)
        self.assertEqual(self.deliveries(), [])

    def test_invalid_cel_and_impossible_adapter_settings_retain_active_configuration(self):
        self.policy()
        self.apply()
        baseline = copy.deepcopy(self.service.status())
        self.bundle["notification_defaults"]["events"]["incident.created"]["when"] = "incident.status =="
        with self.assertRaisesRegex(ContractError, "invalid notification CEL"):
            self.candidate()
        del self.bundle["notification_defaults"]["events"]["incident.created"]["when"]
        del self.bundle["notification_defaults"]["fallbacks"]["thread"]
        with self.assertRaisesRegex(ContractError, "thread.*explicit fallback"):
            self.candidate()
        self.assertEqual(baseline, self.service.status())

    def test_event_none_suppresses_sending_but_keeps_the_incident(self):
        self.bundle["notification_defaults"] = {"events": {"incident.created": {"do": ["none"]}}}
        self.apply()
        self.correlate(self.event("first"))
        self.dispatcher().run_once()
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.sent, [])

    def test_event_condition_is_evaluated_against_canonical_state(self):
        self.bundle["notification_defaults"] = {"events": {
            "incident.created": {"do": ["post"], "when": 'incident.status == "resolved"'}}}
        self.apply()
        self.correlate(self.event("first"))
        self.dispatcher().run_once()
        self.assertEqual(self.sent, [])

    def test_team_override_changes_message_buttons_and_conditional_fields(self):
        self.bundle["notification_defaults"] = {"events": {"incident.created": {"do": ["post"]}},
            "buttons": [{"command": "ack", "label": "Take", "when": 'incident.status == "firing"'},
                        {"command": "resolve", "label": "Finish", "when": 'incident.status == "acknowledged"'}],
            "card": {"fields": [{"path": "normalized.resource", "label": "PVC", "order": 1, "when": "false"}],
                     "footer": "Team {{ incident.team_id }}", "tags": ["{{ incident.status }}"]}}
        self.bundle["notification_policies"] = [{"id": "alpha", "scope": "team", "team_id": "alpha",
            "settings": {"card": {"title": "Configured {{ incident.name }}"}}}]
        self.apply()
        self.correlate(self.event("first"))
        self.dispatcher().run_once()
        self.assertEqual(len(self.sent), 2)
        for message in self.sent:
            self.assertTrue(message["title"].startswith("Configured "))
            self.assertEqual([action["label"] for action in message["actions"]], ["Take"])
            self.assertEqual(message["fields"], [])
            self.assertEqual(message["footer"], "Team alpha")
            self.assertEqual(message["tags"], ["firing"])
