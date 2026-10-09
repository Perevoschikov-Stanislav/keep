"""Test Mattermost bridge interactive actions and /action endpoint."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transport_bridge", ROOT / "transports/mattermost/bridge.py")
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class BridgeActionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "state.db"
        self.token = "test-secret-token"
        os.environ["TEST_TRANSPORT_TOKEN"] = self.token
        os.environ["TEST_SERVICE_TOKEN"] = "test-service-token"
        os.environ["TEST_MM_TOKEN"] = "test-mm-token"
        self.config = {
            "schema_version": 1,
            "tenant_id": "keep",
            "transport_ref": "mattermost-core",
            "keep_api_url": "http://keep-backend:8080",
            "keep_ui_url": "http://localhost:8000",
            "mattermost_url": "http://mattermost:8065",
            "action_url": "http://keep-mm-bridge:8080/action",
            "transport_token_ref": "env:TEST_TRANSPORT_TOKEN",
            "service_token_ref": "env:TEST_SERVICE_TOKEN",
            "mattermost_token_ref": "env:TEST_MM_TOKEN",
            "state_db": str(self.db_path),
            "destinations": {
                "dest-1": {"team_id": "ops", "channel_id": "a" * 26}
            }
        }
        self.bridge = bridge_module.Bridge(self.config)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_render_with_and_without_action_url(self):
        envelope = {
            "notification": {
                "notification_id": "11111111-1111-1111-1111-111111111111",
                "incident_id": "22222222-2222-2222-2222-222222222222",
                "incident_revision": 3,
                "projection_revision": 1,
                "title": "Incident Alert",
                "description": "High CPU",
                "color": "#d00000",
                "fields": [{"label": "Status", "value": "firing"}],
                "links": [{"label": "Keep", "url": "http://keep/incidents/1"}],
                "actions": [
                    {"command": "ack", "label": "Acknowledge", "keep_url": "http://keep?cmd=ack"},
                    {"command": "unack", "label": "Unacknowledge", "keep_url": "http://keep?cmd=unack"}
                ]
            },
            "channel_id": "a" * 26,
            "contacts": ["@operator"]
        }

        # Without action_url
        rendered_plain = bridge_module.Bridge.render(envelope)
        att_plain = rendered_plain["props"]["attachments"][0]
        self.assertNotIn("actions", att_plain)
        self.assertIn("[Acknowledge](http://keep?cmd=ack)", att_plain["footer"])

        # With action_url
        rendered_actions = bridge_module.Bridge.render(envelope, "http://bridge:8080/action", "env:TEST_TRANSPORT_TOKEN")
        att_actions = rendered_actions["props"]["attachments"][0]
        self.assertIn("actions", att_actions)
        self.assertEqual(len(att_actions["actions"]), 2)
        ack_btn = att_actions["actions"][0]
        self.assertEqual(ack_btn["id"], "ack")
        self.assertEqual(ack_btn["name"], "Acknowledge")
        self.assertEqual(ack_btn["style"], "primary")
        self.assertEqual(ack_btn["integration"]["url"], "http://bridge:8080/action")
        self.assertEqual(ack_btn["integration"]["context"]["command"], "ack")
        self.assertEqual(ack_btn["integration"]["context"]["expected_revision"], 3)
        self.assertEqual(ack_btn["integration"]["context"]["secret"], digest(["22222222-2222-2222-2222-222222222222", self.token]))

        unack_btn = att_actions["actions"][1]
        self.assertEqual(unack_btn["id"], "unack")
        self.assertEqual(unack_btn["style"], "default")

    def test_action_ack_and_unack_success(self):
        incident_id = "22222222-2222-2222-2222-222222222222"
        secret_key = digest([incident_id, self.token])

        with patch.object(self.bridge, "keep") as mock_keep:
            mock_keep.return_value = {"status": "ok"}

            # Test ack
            body_ack = {
                "user_name": "stanislav",
                "context": {
                    "incident_id": incident_id,
                    "command": "ack",
                    "expected_revision": 1,
                    "secret": secret_key
                }
            }
            res_ack = self.bridge.action(body_ack)
            self.assertEqual(res_ack, {"ephemeral_text": "Acknowledged"})
            mock_keep.assert_called_once()
            args, kwargs = mock_keep.call_args
            self.assertEqual(args[0], "POST")
            self.assertEqual(args[1], f"/incidents/{incident_id}/commands")
            self.assertEqual(args[2]["command"], "ack")
            self.assertEqual(args[2]["expected_revision"], 1)

            mock_keep.reset_mock()

            # Test unack
            body_unack = {
                "user_name": "stanislav",
                "context": {
                    "incident_id": incident_id,
                    "command": "unack",
                    "expected_revision": 2,
                    "secret": secret_key
                }
            }
            res_unack = self.bridge.action(body_unack)
            self.assertEqual(res_unack, {"ephemeral_text": "Unacknowledged"})
            mock_keep.assert_called_once()
            args, kwargs = mock_keep.call_args
            self.assertEqual(args[2]["command"], "unack")
            self.assertEqual(args[2]["expected_revision"], 2)

    def test_action_unauthorized_and_invalid(self):
        incident_id = "22222222-2222-2222-2222-222222222222"

        # Wrong secret
        body_wrong_secret = {
            "context": {
                "incident_id": incident_id,
                "command": "ack",
                "expected_revision": 1,
                "secret": "wrong-secret"
            }
        }
        res = self.bridge.action(body_wrong_secret)
        self.assertEqual(res, {"ephemeral_text": "Button rejected: unauthorized action"})

        # Missing context
        res_empty = self.bridge.action({})
        self.assertEqual(res_empty, {"ephemeral_text": "Invalid action context"})

    def test_action_silence_and_ticket(self):
        incident_id = "22222222-2222-2222-2222-222222222222"
        secret_key = digest([incident_id, self.token])

        res_silence = self.bridge.action({
            "context": {
                "incident_id": incident_id,
                "command": "silence",
                "expected_revision": 1,
                "secret": secret_key
            }
        })
        self.assertIn("Manage silences in Keep", res_silence["ephemeral_text"])

        res_ticket = self.bridge.action({
            "context": {
                "incident_id": incident_id,
                "command": "ticket",
                "expected_revision": 1,
                "secret": secret_key
            }
        })
        self.assertEqual(res_ticket, {"ephemeral_text": "Ticket action requested"})


if __name__ == "__main__":
    unittest.main()
