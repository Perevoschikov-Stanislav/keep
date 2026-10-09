"""Run in the Keep API image: python tests/test_team_migration_fork.py."""

import importlib.util
import io
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION = (
    Path(__file__).parents[1]
    / "keep/api/models/db/migrations/versions/2026-10-01-00-00_7a4e6f1a2b3c.py"
)


class TeamMigrationTest(unittest.TestCase):
    def test_mysql_upgrade_generates_valid_ddl(self):
        spec = importlib.util.spec_from_file_location("team_migration_mysql", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="mysql", opts={"as_sql": True, "output_buffer": output}
        )
        with Operations.context(context):
            module.upgrade()
        ddl = output.getvalue()
        self.assertIn("ALTER TABLE alert ADD COLUMN team_id VARCHAR(255)", ddl)
        self.assertIn("ALTER TABLE incident ADD COLUMN team_id VARCHAR(255)", ddl)

    def test_upgrade_and_downgrade(self):
        spec = importlib.util.spec_from_file_location("team_migration", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alert (id TEXT PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE incident (id TEXT PRIMARY KEY)"))
            connection.execute(text(
                "CREATE TABLE lastalerttoincident (tenant_id TEXT, incident_id TEXT)"
            ))
            context = MigrationContext.configure(
                connection, opts={"render_as_batch": True}
            )
            with Operations.context(context):
                module.upgrade()
            for table in ("alert", "incident"):
                columns = {item["name"] for item in inspect(connection).get_columns(table)}
                indexes = {item["name"] for item in inspect(connection).get_indexes(table)}
                self.assertIn("team_id", columns)
                self.assertIn(f"ix_{table}_team_id", indexes)
            self.assertIn(
                "idx_lastalerttoincident_tenant_incident",
                {item["name"] for item in inspect(connection).get_indexes("lastalerttoincident")},
            )
            with Operations.context(context):
                module.downgrade()
            for table in ("alert", "incident"):
                columns = {item["name"] for item in inspect(connection).get_columns(table)}
                self.assertNotIn("team_id", columns)
            self.assertNotIn(
                "idx_lastalerttoincident_tenant_incident",
                {item["name"] for item in inspect(connection).get_indexes("lastalerttoincident")},
            )


if __name__ == "__main__":
    unittest.main()
