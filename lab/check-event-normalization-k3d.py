"""Task 25 checks on the existing local k3d, with a private PostgreSQL sidecar."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("fork_k3d_checks", Path(__file__).with_name("check-incident-contract-k3d.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main(suite="event-normalization"))
