"""Offline CLI for the shared incident IaC validator; never applies configuration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keep.api.core.incident_contract import *  # noqa: F403

# Lab callers read fixtures alongside this CLI, including ConfigMap-mounted Jobs.
ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "docs/fork/incident-core"


if __name__ == "__main__":
    main()
