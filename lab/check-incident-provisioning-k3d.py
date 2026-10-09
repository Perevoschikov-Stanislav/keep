"""Run atomic IaC provisioning and regressions in the private k3d test Job."""

import importlib.util
from pathlib import Path

path = Path(__file__).with_name("check-incident-contract-k3d.py")
spec = importlib.util.spec_from_file_location("keep_k3d_check", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main(suite="incident-provisioning"))
