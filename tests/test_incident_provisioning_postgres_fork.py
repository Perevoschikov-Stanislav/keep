"""Apply races and additive migration, using only the private k3d sidecar."""

import importlib.util
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

from keep.api.models.db.tenant import Tenant
from tests import test_incident_provisioning_fork as provisioning_tests

DSN = os.environ.get("KEEP_INTEGRATION_TEST_DATABASE")
if DSN:
    connection = make_url(DSN)
    if connection.host != "127.0.0.1" or connection.database != "silences_check":
        raise RuntimeError("Provisioning checks require the private loopback PostgreSQL sidecar")


@unittest.skipUnless(DSN, "Requires the isolated k3d PostgreSQL sidecar")
class PostgresIncidentProvisioningTest(provisioning_tests.IncidentProvisioningTest):
    def setUp(self):
        schema = "iac_check_" + uuid4().hex
        bootstrap = create_engine(DSN)
        self.addCleanup(bootstrap.dispose)
        with bootstrap.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        def cleanup():
            with bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        self.addCleanup(cleanup)
        engine = create_engine(DSN, connect_args={"options": "-csearch_path=" + schema}, pool_size=5)
        self.enterContext(patch("tests.team_fork_test_case.create_engine", return_value=engine))
        super().setUp()
        with Session(self.engine) as session:
            session.add(Tenant(id="tenant", name="IaC test"))
            session.commit()

    def test_concurrent_first_apply_has_one_winner(self):
        first = self.candidate()
        first_preview = self.service.preview(first)
        self.mapping["priority"] = 10
        self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
        second = self.candidate()
        second_preview = self.service.preview(second)
        result = self.race((first, first_preview), (second, second_preview))
        self.assertEqual(sum(item != "conflict" for item in result), 1)
        self.assertEqual(self.service.status()["generation"], 1)
        self.assertIn(self.service.status()["active_digest"], (first.digest, second.digest))

    def test_concurrent_updates_do_not_overwrite_winner(self):
        self.apply()
        candidates = []
        for priority in (10, 20):
            self.mapping["priority"] = priority
            self.bundle["mappings"][0]["artifact"] = self.artifact("mapping.yaml", self.mapping)
            candidate = self.candidate()
            candidates.append((candidate, self.service.preview(candidate)))
        result = self.race(*candidates)
        self.assertEqual(sum(item != "conflict" for item in result), 1)
        self.assertEqual(self.service.status()["generation"], 2)

    def race(self, *candidates):
        barrier = Barrier(len(candidates))
        def run(pair):
            barrier.wait(timeout=10)
            try:
                return self.apply(*pair)["result"]
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futures = [pool.submit(run, candidate) for candidate in candidates]
            return [future.result(timeout=20) for future in futures]

    def test_actual_migration_retains_data_and_blocks_destructive_downgrade(self):
        from keep.api.models.db.incident_configuration import IncidentConfiguration, IncidentConfigurationVersion, ManagedIncidentResource
        with self.engine.begin() as connection:
            for model in (ManagedIncidentResource, IncidentConfigurationVersion, IncidentConfiguration):
                model.__table__.drop(connection)
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-05-12-00_a83d91ce6f40.py"
        spec = importlib.util.spec_from_file_location("iac_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        self.apply()
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "Configuration history exists"):
                migration.downgrade()
        self.assertIn("incidentconfigurationversion", inspect(self.engine).get_table_names())
