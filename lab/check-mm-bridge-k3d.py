"""Local Mattermost, private PostgreSQL and notification transport checks."""

import importlib.util
import sys
from pathlib import Path


spec = importlib.util.spec_from_file_location("k3d_check", Path(__file__).with_name("check-incident-contract-k3d.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if len(sys.argv) == 1:
    sys.argv.extend(["--skip-build", "--image", "keep-bridge21-check:local", "--bridge-mattermost", "--tests",
        "tests.test_incident_notifications_bridge_fork.BridgeTransportTest",
        "tests.test_incident_notifications_bridge_fork.PostgresBridgeTransportTest",
        "tests.test_incident_notifications_bridge_fork.BridgeConfigurationTest"])
raise SystemExit(module.main("incident-notifications"))
