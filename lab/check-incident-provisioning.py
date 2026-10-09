"""Task 24 checks with an actual private PostgreSQL sidecar, no skipped checks."""

import importlib.util
import sys
from pathlib import Path

path = Path(__file__).with_name("check-silences-integration.py")
spec = importlib.util.spec_from_file_location("fork_checks", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
contract_spec = importlib.util.spec_from_file_location("incident_contract_checks", Path(__file__).with_name("check-incident-contract.py"))
contract = importlib.util.module_from_spec(contract_spec)
sys.modules[contract_spec.name] = contract
contract_spec.loader.exec_module(contract)
raise SystemExit(module.main(module.MODULES + [
    "tests.test_incident_provisioning_fork", "tests.test_incident_provisioning_postgres_fork",
    "incident_contract_checks",
]))
