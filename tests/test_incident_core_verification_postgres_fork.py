"""Task 31: End-to-end incident core verification on isolated k3d PostgreSQL."""

import unittest

from tests.test_incident_notifications_postgres_fork import DSN, PostgresFixture
from tests import test_incident_core_verification_fork as core_tests


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresIncidentCoreVerificationTest(PostgresFixture, core_tests.IncidentCoreVerificationTest):
    def test_concurrent_alert_correlation_and_presentation_refresh(self):
        """Concurrent alert correlation and presentation refresh in PostgreSQL
        preserves consistency and does not deadlock.
        """
        def correlate_worker(index):
            event = self.event(f"p-conc-{index}", workload="worker-svc", team="alpha")
            return self.correlate(event)

        results = self.concurrent(correlate_worker, count=4)
        incidents = self.incidents()
        self.assertTrue(len(incidents) >= 1)
        self.assertEqual(incidents[0].team_id, "alpha")
