"""Backend regressions plus signed proof/outbox tests; executed by the k3d Job."""

import os
import sys
import unittest
from pathlib import Path

Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)

MODULES = [
    "tests.test_team_policy_fork", "tests.test_team_isolation_fork", "tests.test_team_custom_names_fork",
    "tests.test_team_migration_fork", "tests.test_team_backfill_fork", "tests.test_teams_settings_fork",
    "tests.test_silences_api_fork", "tests.test_silences_queries_fork", "tests.test_silences_migration_fork",
    "tests.test_silences_concurrency_fork", "tests.test_silences_notification_dispatch_fork",
    "tests.test_silences_legacy_transition_fork", "tests.test_silences_integration_api_fork",
    "tests.test_silences_outbox_fork", "tests.test_silences_postgres_integration_fork",
    "tests.test_oss_request_context_fork",
]

def main(modules=MODULES):
    if not os.environ.get("KEEP_INTEGRATION_TEST_DATABASE"):
        raise SystemExit("This check requires the isolated PostgreSQL k3d sidecar")
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Unexpected skipped integration checks", file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
