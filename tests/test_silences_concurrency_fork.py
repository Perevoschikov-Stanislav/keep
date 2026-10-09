"""Race real transactions. Optional PostgreSQL target is restricted to a local test DB."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, select

from keep.api.bl.silences_bl import SilencesBL
from keep.api.models.db.alert import Alert, AlertEnrichment, LastAlert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.maintenance_window import MaintenanceWindowRule
from keep.api.models.db.facet import Facet
from keep.api.models.db.rule import Rule
from keep.api.models.db.silence import Silence, SilenceCommand, SilenceEvent
from keep.api.models.db.tenant import Tenant
from keep.api.models.db.user import User
from keep.api.models.silence import CreateSilenceCommand, UpdateSilenceCommand, utc_string
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from tests.team_fork_test_case import TeamPolicyTestCase
from tests.test_silences_api_fork import NOW
from tests.test_silences_migration_fork import migration


class SilenceConcurrencyTest(TeamPolicyTestCase):
    def setUp(self):
        super().setUp()
        target = os.environ.get("KEEP_SILENCES_TEST_POSTGRES")
        if target:
            url = make_url(target)
            if url.get_backend_name() != "postgresql" or url.host or url.database != "silences_test" or url.query.get("host") != "/var/run/postgresql":
                raise ValueError("Only the isolated silences_test database on a Unix socket is accepted")
            schema = "silences_test_" + uuid4().hex
            bootstrap = create_engine(url)
            with bootstrap.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            bootstrap.dispose()
            self.engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        else:
            directory = Path(os.environ.get("TMPDIR", ".lab-work/silences-api"))
            directory.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(f"sqlite:///{directory / ('race-' + uuid4().hex + '.db')}",
                connect_args={"check_same_thread": False, "timeout": 15})
        self.addCleanup(self.engine.dispose)
        base_models = (Tenant, User, Rule, Incident, Alert, AlertEnrichment, LastAlert, LastAlertToIncident, Facet, MaintenanceWindowRule)
        SQLModel.metadata.create_all(self.engine, tables=[model.__table__ for model in base_models])
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            migration(Path(__file__).resolve().parents[1] /
                      "keep/api/models/db/migrations/versions/2026-10-07-10-00_e73b914a620c.py").upgrade()
        with Session(self.engine) as session:
            session.add(Tenant(id="keep", name="Test"))
            session.commit()
            session.add(User(id=1, tenant_id="keep", username="responder@example.test", password_hash="unused", role="responder"))
            alert = Alert(tenant_id="keep", team_id="alpha", fingerprint="a", timestamp=NOW,
                provider_type="test", provider_id="test", event={"name": "a", "status": "firing", "severity": "high", "lastReceived": utc_string(NOW)})
            session.add(alert)
            session.flush()
            session.add(LastAlert(tenant_id="keep", fingerprint="a", alert_id=alert.id, timestamp=NOW, first_timestamp=NOW))
            session.commit()
        self.entity = AuthenticatedEntity(tenant_id="keep", email="responder@example.test", role="responder",
            teams=frozenset({"alpha"}), visible_teams=frozenset({"alpha"}))
        self.command = CreateSilenceCommand(schema_version=1, client_request_id=uuid4(), team_id="alpha",
            selector={"kind": "alert", "fingerprints": ["a"]}, starts_at=None, ends_at=None,
            comment="Maintenance", correlation_id=None)

    def race(self, operation):
        barrier = threading.Barrier(2)
        def run(index):
            with Session(self.engine) as session:
                barrier.wait(timeout=10)
                try:
                    response, status = operation(SilencesBL(session, self.entity, NOW), index)
                    return status, response
                except HTTPException as exc:
                    return exc.status_code, exc.detail
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, index) for index in range(2)]
            return [future.result(timeout=20) for future in futures]

    def counts(self):
        with Session(self.engine) as session:
            return tuple(len(session.exec(select(model)).all()) for model in (Silence, SilenceCommand, SilenceEvent))

    def test_same_create_nonce_has_one_rule_and_one_audit(self):
        results = self.race(lambda bl, index: bl.create(self.command))
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(len({response.result.id for _, response in results}), 1)
        self.assertEqual(sorted(response.replayed for _, response in results), [False, True])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_update_nonce_replays_after_waiting_for_lock(self):
        with Session(self.engine) as session:
            rule = SilencesBL(session, self.entity, NOW).create(self.command)[0].result
        command = UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
            changes={"comment": "Changed"}, correlation_id=None)
        results = self.race(lambda bl, index: bl.update(rule.id, command))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sorted(response.replayed for _, response in results), [False, True])
        self.assertEqual(self.counts(), (1, 2, 2))

    def test_stale_concurrent_update_does_not_overwrite_winner(self):
        with Session(self.engine) as session:
            rule = SilencesBL(session, self.entity, NOW).create(self.command)[0].result
        commands = [UpdateSilenceCommand(schema_version=1, client_request_id=uuid4(), expected_revision=1,
            changes={"comment": f"writer-{index}"}, correlation_id=None) for index in range(2)]
        results = self.race(lambda bl, index: bl.update(rule.id, commands[index]))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(response.result for status, response in results if status == 200)
        with Session(self.engine) as session:
            saved = session.get(Silence, rule.id)
            self.assertEqual((saved.comment, saved.revision), (winner.comment, 2))
        self.assertEqual(self.counts(), (1, 2, 2))

    def test_derived_sql_filter_and_facets_on_actual_database(self):
        from keep.api.core import alerts, db, facets, incidents
        from keep.api.core.cel_to_sql.sql_providers import get_cel_to_sql_provider_for_dialect as providers
        from keep.api.core.facets_query_builder import get_facets_query_builder as facet_builder
        from keep.api.models.facet import FacetOptionsQueryDto
        from keep.api.models.query import QueryDto

        for module in (alerts, db, facets, incidents, providers, facet_builder):
            self.enterContext(patch.object(module, "engine", self.engine))
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))
        with Session(self.engine) as session:
            SilencesBL(session, self.entity, NOW).create(self.command)
        rows, total = alerts.query_last_alerts("keep", QueryDto(cel="silence.silenced == true"), frozenset({"alpha"}))
        self.assertEqual((total, [row.fingerprint for row in rows]), (1, ["a"]))
        facet = next(item for item in alerts.static_facets if item.property_path == "dismissed")
        options = alerts.get_alert_facets_data("keep", FacetOptionsQueryDto(cel="", facet_queries={facet.id: ""}), frozenset({"alpha"}))[facet.id]
        self.assertEqual([(option.value, option.matches_count) for option in options], [(True, 1)])

    def test_legacy_dismiss_race_owns_one_dedicated_rule(self):
        from keep.api.routes.alerts import _handle_legacy_silence_compatibility
        def operation(bl, index):
            _handle_legacy_silence_compatibility(bl.session, self.entity, ["a"],
                                               {"dismissed": True, "note": "legacy race"})
            rule = bl.session.exec(select(Silence)).one()
            return bl.get(rule.id), 200
        results = self.race(operation)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(len({response.id for _, response in results}), 1)
        self.assertEqual(self.counts(), (1, 2, 1))

    def test_legacy_migration_race_does_not_duplicate_import(self):
        from keep.api.bl.silences_migration_bl import SilencesMigrationBL
        with Session(self.engine) as session:
            session.add(AlertEnrichment(tenant_id="keep", alert_fingerprint="a",
                                       enrichments={"dismissed": True}))
            session.commit()
        results = self.race(lambda bl, index: (
            SilencesMigrationBL(bl.session).apply("keep", include_maintenance=False), 200,
        ))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sorted(response["dismissals_migrated"] for _, response in results), [0, 1])
        self.assertEqual(self.counts(), (1, 0, 1))
