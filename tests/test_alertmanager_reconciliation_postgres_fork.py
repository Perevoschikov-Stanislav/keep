"""Alertmanager active reconciliation integration tests on isolated PostgreSQL."""

import unittest
import importlib.util
from pathlib import Path
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlmodel import Session, select

from keep.api.models.db.silence import Silence, AlertmanagerReconciliationState
from keep.api.models.silence import utc_now

from tests.test_incident_notifications_postgres_fork import DSN, PostgresFixture
from tests import test_alertmanager_reconciliation_fork as am_tests
from tests import test_alertmanager_sync_fork as sync_tests


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresAlertmanagerReconciliationTest(PostgresFixture, am_tests.AlertmanagerReconciliationTest):
    pass


@unittest.skipUnless(DSN, "Requires isolated k3d PostgreSQL")
class PostgresAlertmanagerSyncTest(PostgresFixture, sync_tests.AlertmanagerSyncTest):
    def test_eight_workers_export_one_rule(self):
        with Session(self.engine) as session:
            self.native(session)
        def run(_):
            with Session(self.engine) as session:
                return self.reconciler().sync_keep_silences_to_am(session, [])
        self.concurrent(run)
        self.assertEqual(self.http.post.call_count, 1)
        self.assertEqual(len(self.remote), 1)

    def test_lost_lease_fences_late_response_and_next_worker_recovers_intent(self):
        first = self.reconciler()
        def lose_lease(*args, **kwargs):
            response = self.post(*args, **kwargs)
            with Session(self.engine) as other:
                state = other.get(AlertmanagerReconciliationState, ("tenant", first.source_id))
                state.lease_token, state.lease_until = uuid4(), utc_now() - timedelta(seconds=1)
                other.add(state)
                other.commit()
            return response
        self.http.post.side_effect = lose_lease
        with Session(self.engine) as session:
            identifier = self.native(session)
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                first.sync_keep_silences_to_am(session, [])
            row = session.get(Silence, identifier)
            self.assertEqual(row.external_context[first.source_id]["entries"].popitem()[1]["state"], "uncertain")
            self.http.post.side_effect = self.post
            self.reconciler().sync_keep_silences_to_am(session, self.remote)
        self.assertEqual(self.http.post.call_count, 1)

    def test_lost_lease_rolls_back_import_and_its_event_together(self):
        rec = self.reconciler()
        emit = rec._emit
        def lose_before_commit(*args, **kwargs):
            with Session(self.engine) as other:
                state = other.get(AlertmanagerReconciliationState, ("tenant", rec.source_id))
                state.lease_token = uuid4()
                other.add(state)
                other.commit()
            return emit(*args, **kwargs)
        with Session(self.engine) as session, patch.object(rec, "_emit", side_effect=lose_before_commit):
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                rec.reconcile_silences(session, [self.am()])
            self.assertEqual(session.exec(select(Silence)).all(), [])

    def test_lost_lease_cannot_overwrite_shared_poll_counters(self):
        rec = self.reconciler()
        def lose_during_fetch():
            with Session(self.engine) as other:
                state = other.get(AlertmanagerReconciliationState, ("tenant", rec.source_id))
                state.lease_token, state.alert_state = uuid4(), {"last_count": 42}
                other.add(state)
                other.commit()
            return []
        with Session(self.engine) as session, patch.object(rec, "fetch_alerts", side_effect=lose_during_fetch), \
                patch.object(rec, "fetch_silences", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                rec.reconcile_once(session)
            state = session.get(AlertmanagerReconciliationState, ("tenant", rec.source_id))
            self.assertEqual(state.alert_state, {"last_count": 42})

    def test_additive_migration_preserves_existing_silence_and_refuses_receipt_loss(self):
        with Session(self.engine) as session:
            identifier = self.native(session, correlation_id="ordinary-correlation")
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE alertmanagerreconciliationstate"))
            connection.execute(text("ALTER TABLE silence DROP COLUMN external_context"))
        path = Path(__file__).resolve().parents[1] / "keep/api/models/db/migrations/versions/2026-10-07-10-00_e73b914a620c.py"
        spec = importlib.util.spec_from_file_location("am_sync_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with Session(self.engine) as session:
            row = session.get(Silence, identifier)
            self.assertEqual(row.comment, "review")
            self.assertEqual(row.correlation_id, "ordinary-correlation")
            self.assertEqual(row.external_context, {})
            self.assertEqual(row.revision, 1)
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        with Session(self.engine) as session:
            self.reconciler().sync_keep_silences_to_am(session, [])
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with self.assertRaisesRegex(RuntimeError, "review external silences"):
                migration.downgrade()
