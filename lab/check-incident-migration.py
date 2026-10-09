"""Task 30 plus all preceding fork regressions, on private k3d PostgreSQL."""

import importlib.util
import json
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("fork_checks", Path(__file__).with_name("check-silences-integration.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
contract_spec = importlib.util.spec_from_file_location("incident_contract_checks", Path(__file__).with_name("check-incident-contract.py"))
contract = importlib.util.module_from_spec(contract_spec)
sys.modules[contract_spec.name] = contract
contract_spec.loader.exec_module(contract)
selected = json.loads(os.environ.get("KEEP_CHECK_TESTS", "[]"))
raise SystemExit(module.main(selected or module.MODULES + [
    "tests.test_incident_provisioning_fork", "tests.test_incident_provisioning_postgres_fork", "incident_contract_checks",
    "tests.test_event_normalization_fork", "tests.test_event_normalization_postgres_fork",
    "tests.test_incident_correlation_fork", "tests.test_incident_correlation_postgres_fork",
    "tests.test_incident_lifecycle_fork", "tests.test_incident_lifecycle_postgres_fork",
    "tests.test_incident_automation_fork", "tests.test_incident_automation_postgres_fork",
    "tests.test_incident_notifications_fork", "tests.test_incident_notifications_api_fork", "tests.test_incident_notifications_postgres_fork",
    "tests.test_legacy_incident_migration_fork", "tests.test_legacy_incident_migration_postgres_fork",
]))
