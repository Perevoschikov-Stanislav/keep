"""Tests for Task 19: Silences Legacy Migration and Compatibility Layer."""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from keep.api.bl.dismissal_expiry_bl import DismissalExpiryBl
from keep.api.bl.enrichments_bl import EnrichmentsBl
from keep.api.bl.silences_bl import SilencesBL
from keep.api.bl.silences_migration_bl import SilencesMigrationBL
from keep.api.core.db import get_session
from keep.identitymanager.identitymanagerfactory import IdentityManagerFactory
from keep.api.models.alert import AlertStatus
from keep.api.models.db.alert import Alert, AlertEnrichment, LastAlert
from keep.api.models.db.maintenance_window import MaintenanceWindowRule
from keep.api.models.db.silence import Silence, SilenceCommand, SilenceEvent
from keep.api.models.db.tenant import Tenant
from keep.api.models.db.user import User
from keep.api.models.silence import (
    AlertSelector,
    CreateSilenceCommand,
    FilterSelector,
    utc_string,
)
from keep.api.routes import alerts
from keep.identitymanager.authenticatedentity import AuthenticatedEntity
from tests.team_fork_test_case import TeamDatabaseTestCase

NOW = datetime(2026, 10, 4, 12, 0, 0)


class SilencesLegacyTransitionTest(TeamDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as session:
            session.add(Tenant(id="keep", name="Test"))
            session.add(Tenant(id="another", name="Other"))
            session.add(
                User(
                    id=1,
                    tenant_id="keep",
                    username="member@example.test",
                    password_hash="unused",
                    role="responder",
                )
            )
            session.add(
                User(
                    id=2,
                    tenant_id="keep",
                    username="admin@example.test",
                    password_hash="unused",
                    role="admin",
                )
            )
            session.commit()

        self.responder_alpha = AuthenticatedEntity(
            tenant_id="keep",
            email="member@example.test",
            role="responder",
            teams=frozenset({"alpha"}),
            visible_teams=frozenset({"alpha"}),
        )
        self.admin = AuthenticatedEntity(
            tenant_id="keep", email="admin@example.test", role="admin"
        )

        self.enterContext(patch("keep.api.bl.silences_bl.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.bl.silences_migration_bl.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.routes.alerts.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))
        self.enterContext(patch("keep.api.core.elastic.ElasticClient"))
        self.enterContext(patch("keep.api.core.dependencies.get_pusher_client"))

    def _seed_alert(
        self,
        fingerprint: str,
        team_id: str | None,
        *,
        status: str = "firing",
        tenant: str = "keep",
        enrichments: dict | None = None,
    ):
        with Session(self.engine) as session:
            alert = Alert(
                tenant_id=tenant,
                team_id=team_id,
                fingerprint=fingerprint,
                alert_hash=f"hash_{fingerprint}",
                timestamp=NOW,
                provider_type="test",
                provider_id="test",
                event={
                    "name": fingerprint,
                    "fingerprint": fingerprint,
                    "status": status,
                    "lastReceived": utc_string(NOW),
                    "severity": "high",
                    "team_id": team_id,
                },
            )
            session.add(alert)
            session.flush()

            last = session.get(LastAlert, (tenant, fingerprint))
            if last:
                last.alert_id = alert.id
                last.timestamp = NOW
                session.add(last)
            else:
                session.add(
                    LastAlert(
                        tenant_id=tenant,
                        fingerprint=fingerprint,
                        alert_id=alert.id,
                        timestamp=NOW,
                        first_timestamp=NOW,
                    )
                )

            if enrichments:
                enr = AlertEnrichment(
                    tenant_id=tenant,
                    alert_fingerprint=fingerprint,
                    enrichments=enrichments,
                )
                session.add(enr)

            session.commit()
            return alert.id

    def _create_test_client(self):
        app = FastAPI()
        app.include_router(alerts.router, prefix="/alerts")

        def override_session():
            with Session(self.engine) as s:
                yield s

        app.dependency_overrides[get_session] = override_session
        fresh_verifier = IdentityManagerFactory.get_auth_verifier(["update:alert"])
        for route in app.routes:
            if hasattr(route, "dependant"):
                for dep in route.dependant.dependencies:
                    if hasattr(dep.call, "scopes"):
                        app.dependency_overrides[dep.call] = fresh_verifier

        return TestClient(app)

    def test_inventory_dry_run_does_not_mutate_database(self):
        # 1. Seed legacy data
        # fp_active: active with status=suppressed and disposable flags
        self._seed_alert(
            "fp_active",
            "alpha",
            enrichments={
                "dismissed": True,
                "dismissUntil": utc_string(NOW + timedelta(hours=2)),
                "status": "suppressed",
                "disposable_dismissed": True,
                "disposable_dismissUntil": True,
                "note": "Investigating active issue",
            },
        )
        # fp_forever: indefinite dismiss
        self._seed_alert(
            "fp_forever",
            "beta",
            enrichments={
                "dismissed": True,
                "dismissUntil": "forever",
                "status": "suppressed",
            },
        )
        # fp_expired: expired dismissal
        self._seed_alert(
            "fp_expired",
            "alpha",
            enrichments={
                "dismissed": True,
                "dismissUntil": utc_string(NOW - timedelta(hours=1)),
            },
        )

        with Session(self.engine) as session:
            # Seed maintenance rules
            m_active = MaintenanceWindowRule(
                tenant_id="keep",
                name="Database Upgrade",
                cel_query="alert.name == 'db'",
                start_time=NOW - timedelta(hours=1),
                end_time=NOW + timedelta(hours=2),
                enabled=True,
                created_by="infra-team",
            )
            m_expired = MaintenanceWindowRule(
                tenant_id="keep",
                name="Old Window",
                cel_query="alert.name == 'old'",
                start_time=NOW - timedelta(hours=5),
                end_time=NOW - timedelta(hours=1),
                enabled=True,
                created_by="infra-team",
            )
            session.add(m_active)
            session.add(m_expired)
            session.commit()

            # 2. Run inventory (dry-run)
            migration_bl = SilencesMigrationBL(session)
            report = migration_bl.inventory(tenant_id="keep")

            # 3. Assert report stats
            d = report["dismissals"]
            self.assertEqual(d["total"], 3)
            self.assertEqual(d["active_migratable"], 2)
            self.assertEqual(d["expired"], 1)
            self.assertEqual(d["indefinite"], 1)
            self.assertEqual(d["with_suppressed_override"], 2)
            self.assertEqual(d["with_disposable_flags"], 1)

            m = report["maintenance_rules"]
            self.assertEqual(m["total"], 2)
            self.assertEqual(m["active_migratable"], 1)
            self.assertEqual(m["expired_or_disabled"], 1)
            self.assertEqual(m["unassigned_team_rules"], 2)

            self.assertEqual(len(report["admin_action_required"]), 3)
            maintenance_review = [action for action in report["admin_action_required"]
                                  if action["type"] == "maintenance_team_unassigned"]
            self.assertEqual(maintenance_review[0]["rule_name"], "Database Upgrade")

            # 4. Assert DB is completely untouched
            silences = session.exec(select(Silence)).all()
            self.assertEqual(len(silences), 0)

            silence_events = session.exec(select(SilenceEvent)).all()
            self.assertEqual(len(silence_events), 0)

            # Ensure enrichments still have status=suppressed and disposable flags
            enr_active = session.exec(
                select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "fp_active")
            ).first()
            self.assertEqual(enr_active.enrichments.get("status"), "suppressed")
            self.assertTrue(enr_active.enrichments.get("disposable_dismissed"))

    def test_apply_migration_idempotent_and_cleans_suppressed_and_disposable(self):
        # 1. Seed legacy dismissals
        self._seed_alert(
            "fp_active",
            "alpha",
            enrichments={
                "dismissed": True,
                "dismissUntil": utc_string(NOW + timedelta(hours=2)),
                "status": "suppressed",
                "disposable_dismissed": True,
                "note": "Legacy active note",
            },
        )
        self._seed_alert(
            "fp_forever",
            "beta",
            enrichments={
                "dismissed": True,
                "dismissUntil": "forever",
                "status": "suppressed",
            },
        )

        with Session(self.engine) as session:
            m_rule = MaintenanceWindowRule(
                tenant_id="keep",
                name="Network Window",
                cel_query="name == 'network'",
                start_time=NOW - timedelta(minutes=10),
                end_time=NOW + timedelta(hours=1),
                enabled=True,
                created_by="admin@example.test",
            )
            session.add(m_rule)
            session.commit()
            m_rule_id = m_rule.id

            migration_bl = SilencesMigrationBL(session)

            # 2. First apply
            assignments = {"keep": {str(m_rule_id): {"teams": ["alpha"], "cel": "name == 'network'"}}}
            res1 = migration_bl.apply(tenant_id="keep", include_maintenance=True,
                                     maintenance_teams=assignments)
            self.assertEqual(res1["dismissals_migrated"], 2)
            self.assertEqual(res1["maintenance_rules_migrated"], 1)
            self.assertEqual(res1["suppressed_overrides_cleared"], 0)
            self.assertEqual(res1["disposable_flags_cleaned"], 1)
            self.assertEqual(len(res1["created_silence_ids"]), 3)

            # Check created Silence records
            silences = session.exec(select(Silence).order_by(Silence.created_at)).all()
            self.assertEqual(len(silences), 3)

            s_active = session.exec(
                select(Silence).where(Silence.correlation_id == "legacy-dismiss:fp_active")
            ).first()
            self.assertIsNotNone(s_active)
            self.assertEqual(s_active.team_id, "alpha")
            self.assertEqual(s_active.selector, {"kind": "alert", "fingerprints": ["fp_active"]})
            self.assertEqual(s_active.origin, "legacy-migration")

            s_forever = session.exec(
                select(Silence).where(Silence.correlation_id == "legacy-dismiss:fp_forever")
            ).first()
            self.assertIsNotNone(s_forever)
            self.assertIsNone(s_forever.ends_at)
            self.assertEqual(s_forever.team_id, "beta")

            s_maint = session.exec(
                select(Silence).where(Silence.correlation_id == f"legacy-maintenance:{m_rule_id}")
            ).first()
            self.assertIsNotNone(s_maint)
            self.assertEqual(s_maint.team_id, "alpha")
            self.assertEqual(s_maint.selector, {"kind": "filter", "cel": "name == 'network'"})

            # Check SilenceEvent audit rows
            events = session.exec(select(SilenceEvent)).all()
            self.assertEqual(len(events), 3)
            for ev in events:
                self.assertEqual(ev.event_type, "silence.created")
                self.assertEqual(ev.revision, 1)

            # Check AlertEnrichment cleanup
            enr_active = session.exec(
                select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "fp_active")
            ).first()
            self.assertEqual(enr_active.enrichments["status"], "suppressed")
            self.assertNotIn("dismissed", enr_active.enrichments)
            self.assertNotIn("disposable_dismissed", enr_active.enrichments)

            # 3. Second apply (Idempotency test)
            res2 = migration_bl.apply(tenant_id="keep", include_maintenance=True,
                                     maintenance_teams=assignments)
            self.assertEqual(res2["dismissals_migrated"], 0)
            self.assertEqual(res2["dismissals_skipped_already_present"], 0)
            self.assertEqual(res2["maintenance_rules_migrated"], 0)
            self.assertEqual(res2["maintenance_rules_skipped_already_present"], 1)
            self.assertEqual(res2["suppressed_overrides_cleared"], 0)
            self.assertEqual(res2["disposable_flags_cleaned"], 0)
            self.assertEqual(len(res2["created_silence_ids"]), 0)
            self.assertEqual(len(res2["admin_action_required"]), 2)
            self.assertTrue(all(action["type"] == "dismiss_status_provenance_required"
                                for action in res2["admin_action_required"]))

            # Silence and SilenceEvent counts must remain 3
            self.assertEqual(len(session.exec(select(Silence)).all()), 3)
            self.assertEqual(len(session.exec(select(SilenceEvent)).all()), 3)

    def test_compatibility_api_dismiss_creates_silence_and_preserves_status(self):
        # Seed an alert belonging to alpha
        alert_id = self._seed_alert("fp_compat_1", "alpha", status="firing")

        client = self._create_test_client()
        headers = {
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": "/roles/responder, /teams/alpha",
        }

        # Call POST /alerts/fp_compat_1/enrich with dismissed: true
        body = {
            "fingerprint": "fp_compat_1",
            "enrichments": {
                "dismissed": True,
                "dismissUntil": utc_string(NOW + timedelta(hours=3)),
                "note": "Dismissed through legacy enrich",
            },
        }
        res = client.post("/alerts/enrich", json=body, headers=headers)
        self.assertEqual(res.status_code, 200, res.text)

        with Session(self.engine) as session:
            # Silence created in registry
            silence = session.exec(select(Silence)).first()
            self.assertIsNotNone(silence)
            self.assertEqual(silence.team_id, "alpha")
            self.assertEqual(silence.selector, {"kind": "alert", "fingerprints": ["fp_compat_1"]})
            self.assertEqual(silence.comment, "Dismissed through legacy enrich")

            # Real alert status remains 'firing' (never overridden to suppressed!)
            db_alert = session.get(Alert, alert_id)
            self.assertEqual(db_alert.event["status"], "firing")

            # AlertEnrichment does not have status='suppressed'
            enr = session.exec(
                select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "fp_compat_1")
            ).first()
            self.assertNotEqual(enr.enrichments.get("status"), "suppressed")

    def test_compatibility_api_team_isolation(self):
        # Alert belonging to beta
        self._seed_alert("fp_beta", "beta", status="firing")

        client = self._create_test_client()
        # Member belongs to alpha only
        alpha_headers = {
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": "/roles/responder, /teams/alpha",
        }

        body = {
            "fingerprint": "fp_beta",
            "enrichments": {"dismissed": True},
        }
        # Attempt to dismiss foreign alert must be rejected
        res = client.post("/alerts/enrich", json=body, headers=alpha_headers)
        self.assertIn(res.status_code, (403, 404))

        with Session(self.engine) as session:
            # No silence rule created
            self.assertEqual(len(session.exec(select(Silence)).all()), 0)

    def test_compatibility_restore_never_cancels_filter_or_incident_rules(self):
        # Alert fp_restore in team alpha
        self._seed_alert("fp_restore", "alpha", status="firing")

        with Session(self.engine) as session:
            # 1. Active CEL filter silence rule (e.g. maintenance window)
            s_filter = SilencesBL(session, self.admin, NOW).create(
                CreateSilenceCommand(
                    schema_version=1,
                    team_id="alpha",
                    selector=FilterSelector(kind="filter", cel="alert.name == 'fp_restore'"),
                    starts_at=None,
                    ends_at=utc_string(NOW + timedelta(hours=2)),
                    comment="Filter rule",
                    correlation_id=None,
                    client_request_id=uuid4(),
                )
            )[0].result

            # 2. Active single alert silence rule
            s_alert = SilencesBL(session, self.admin, NOW).create(
                CreateSilenceCommand(
                    schema_version=1,
                    team_id="alpha",
                    selector=AlertSelector(kind="alert", fingerprints=["fp_restore"]),
                    starts_at=None,
                    ends_at=utc_string(NOW + timedelta(hours=2)),
                    comment="Alert rule",
                    correlation_id=None,
                    client_request_id=uuid4(),
                )
            )[0].result

        client = self._create_test_client()
        headers = {
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": "/roles/responder, /teams/alpha",
        }

        # 3. Call legacy restore: dismissed: false
        res = client.post(
            "/alerts/enrich",
            json={"fingerprint": "fp_restore", "enrichments": {"dismissed": False}},
            headers=headers,
        )
        self.assertEqual(res.status_code, 200, res.text)

        with Session(self.engine) as session:
            # Independently created alert rules also survive legacy Restore.
            rule_alert = session.get(Silence, s_alert.id)
            self.assertIsNone(rule_alert.cancelled_at)

            # CEL filter rule MUST REMAIN ACTIVE (NEVER cancelled by single alert restore!)
            rule_filter = session.get(Silence, s_filter.id)
            self.assertIsNone(rule_filter.cancelled_at)

    def test_dispose_on_new_alert_does_not_cancel_silence_or_set_disposable_dismiss(self):
        self._seed_alert("fp_disp", "alpha", status="firing")

        client = self._create_test_client()
        headers = {
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": "/roles/responder, /teams/alpha",
        }

        # Enrich with dispose_on_new_alert=True
        res = client.post(
            "/alerts/enrich?dispose_on_new_alert=true",
            json={
                "fingerprint": "fp_disp",
                "enrichments": {
                    "dismissed": True,
                    "note": "Some note",
                },
            },
            headers=headers,
        )
        self.assertEqual(res.status_code, 200, res.text)

        with Session(self.engine) as session:
            enr = session.exec(
                select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "fp_disp")
            ).first()
            # disposable_dismissed must NOT be created
            self.assertNotIn("disposable_dismissed", enr.enrichments)
            self.assertNotIn("disposable_dismissUntil", enr.enrichments)
            # Other fields (e.g. note) can have disposable_note
            self.assertIn("disposable_note", enr.enrichments)

            # Silence exists and active
            silence = session.exec(select(Silence)).first()
            self.assertIsNotNone(silence)
            self.assertIsNone(silence.cancelled_at)

            # Simulate new alert arrival: calling dispose_enrichments
            enrichments_bl = EnrichmentsBl("keep", db=session)
            enrichments_bl.dispose_enrichments("fp_disp")

            # Check that silence rule was NOT cancelled
            session.refresh(silence)
            self.assertIsNone(silence.cancelled_at)

            # The registry owns the flag; raw legacy fields cannot outlive cancellation.
            session.refresh(enr)
            self.assertNotIn("dismissed", enr.enrichments)
            # But disposable note was cleaned up
            self.assertNotIn("disposable_note", enr.enrichments)
            self.assertNotIn("note", enr.enrichments)

    def test_dismissal_expiry_skips_fingerprints_managed_by_silence_registry(self):
        # Alert with expired legacy timestamp, but migrated to registry
        self._seed_alert(
            "fp_migrated",
            "alpha",
            enrichments={
                "dismissed": True,
                "dismissUntil": utc_string(NOW - timedelta(minutes=10)),
            },
        )
        with Session(self.engine) as session:
            # Silence created with correlation_id
            session.add(
                Silence(
                    id=uuid4(),
                    tenant_id="keep",
                    team_id="alpha",
                    revision=1,
                    selector={"kind": "alert", "fingerprints": ["fp_migrated"]},
                    starts_at=NOW - timedelta(hours=1),
                    ends_at=NOW + timedelta(hours=1),
                    comment="Migrated silence",
                    created_by={"kind": "user", "subject": "admin", "issuer": None, "display_name": "admin"},
                    updated_by={"kind": "user", "subject": "admin", "issuer": None, "display_name": "admin"},
                    created_at=NOW,
                    updated_at=NOW,
                    cancelled_at=None,
                    origin="legacy-migration",
                    correlation_id="legacy-dismiss:fp_migrated",
                    last_event_state="active",
                )
            )
            session.commit()

            # DismissalExpiryBl should skip fp_migrated to avoid competing expiry
            expired = DismissalExpiryBl.get_alerts_with_expired_dismissals(session)
            self.assertEqual(len(expired), 0)
            rule = session.exec(select(Silence)).one()
            # A client-controlled correlation ID does not prove legacy ownership.
            rule.origin = "keep-api"
            session.add(rule)
            session.commit()
            expired = DismissalExpiryBl.get_alerts_with_expired_dismissals(session)
            self.assertEqual([row.alert_fingerprint for row in expired], ["fp_migrated"])

    def test_batch_enrich_alerts_compatibility(self):
        self._seed_alert("fp_batch_1", "alpha", status="firing")
        self._seed_alert("fp_batch_2", "alpha", status="firing")

        client = self._create_test_client()
        headers = {
            "x-forwarded-email": "member@example.test",
            "x-forwarded-groups": "/roles/responder, /teams/alpha",
        }

        body = {
            "fingerprints": ["fp_batch_1", "fp_batch_2"],
            "enrichments": {
                "dismissed": True,
                "note": "Batch dismiss test",
            },
        }
        res = client.post("/alerts/batch_enrich", json=body, headers=headers)
        self.assertEqual(res.status_code, 200, res.text)

        with Session(self.engine) as session:
            silences = session.exec(select(Silence).order_by(Silence.created_at)).all()
            self.assertEqual(len(silences), 2)
            fps = {s.selector["fingerprints"][0] for s in silences}
            self.assertEqual(fps, {"fp_batch_1", "fp_batch_2"})
            for s in silences:
                self.assertEqual(s.team_id, "alpha")
                self.assertEqual(s.comment, "Batch dismiss test")

    def test_maintenance_cutover_preserves_events_in_the_actual_pipeline(self):
        from keep.api.models.alert import AlertDto
        from keep.api.tasks import process_event_task

        alert = AlertDto(id="a" * 32, name="maintenance alert", fingerprint="maintenance",
                         status="firing", lastReceived=utc_string(NOW), severity="high", source=["test"])
        # Stop immediately after the maintenance phase. This exercises the actual
        # ingestion function without running persistence/workflows a second time.
        with Session(self.engine) as session, \
             patch.object(process_event_task, "KEEP_MAINTENANCE_WINDOWS_ENABLED", True), \
             patch.object(process_event_task, "MaintenanceWindowsBl") as maintenance, \
             patch.object(process_event_task, "AlertDeduplicator", side_effect=RuntimeError("reached deduplication")) as dedup:
            maintenance.return_value.maintenance_rules = [MagicMock()]
            maintenance.return_value.check_if_alert_in_maintenance_windows.return_value = True
            handle = getattr(process_event_task, "__handle_formatted_events")
            with patch.object(process_event_task, "KEEP_MAINTENANCE_DESTRUCTIVE_DROP", False):
                with self.assertRaisesRegex(RuntimeError, "reached deduplication"):
                    handle("keep", "test", session, [], [alert], MagicMock())
                maintenance.assert_not_called()
                self.assertEqual(alert.status, "firing")
                dedup.assert_called_once()
            dedup.reset_mock()
            with patch.object(process_event_task, "KEEP_MAINTENANCE_DESTRUCTIVE_DROP", True):
                handle("keep", "test", session, [], [alert], MagicMock())
                maintenance.return_value.check_if_alert_in_maintenance_windows.assert_called_once_with(alert)
                dedup.assert_not_called()

    def test_old_maintenance_recovery_and_writes_are_disabled_after_cutover(self):
        import logging
        from keep.api.bl import maintenance_windows_bl
        from keep.api.routes import maintenance

        with patch.object(maintenance_windows_bl, "config", return_value=False), \
             patch.object(maintenance_windows_bl, "get_session_sync") as get_session, \
             patch.object(maintenance_windows_bl, "recover_prev_alert_status") as recover:
            maintenance_windows_bl.MaintenanceWindowsBl.recover_strategy(logging.getLogger(__name__))
            get_session.assert_not_called()
            recover.assert_not_called()
        app = FastAPI()
        app.include_router(maintenance.router, prefix="/maintenance")
        with patch.object(maintenance, "config", return_value=False):
            for method, path in (("post", "/maintenance"), ("put", "/maintenance/1")):
                response = getattr(TestClient(app), method)(path, json={})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], "legacy_maintenance_disabled")

    def test_migration_invalid_time_is_not_indefinite(self):
        self._seed_alert("bad-time", "alpha", enrichments={"dismissed": True, "dismissUntil": "broken"})
        with Session(self.engine) as session:
            migration = SilencesMigrationBL(session)
            report = migration.inventory("keep")
            self.assertEqual(report["dismissals"]["active_migratable"], 0)
            self.assertTrue(report["dismissals"]["items"][0]["invalid_time"])
            result = migration.apply("keep")
            self.assertEqual(result["dismissals_migrated"], 0)
            self.assertEqual(len(session.exec(select(Silence)).all()), 0)
            self.assertTrue(session.exec(select(AlertEnrichment)).one().enrichments["dismissed"])

    def test_migration_uses_all_canonical_owners(self):
        self._seed_alert("mixed", "alpha", enrichments={"dismissed": True})
        self._seed_alert("mixed", "beta")
        self._seed_alert("unassigned", None, enrichments={"dismissed": True})
        with Session(self.engine) as session:
            alert = session.exec(select(Alert).where(Alert.fingerprint == "unassigned")).one()
            alert.event = {**alert.event, "team_id": "alpha"}
            session.add(alert)
            session.commit()
            result = SilencesMigrationBL(session).apply("keep")
            self.assertEqual(result["dismissals_migrated"], 1)
            rule = session.exec(select(Silence)).one()
            self.assertEqual(rule.selector["fingerprints"], ["unassigned"])
            self.assertIsNone(rule.team_id)
            self.assertEqual(rule.created_by["kind"], "service")
            self.assertEqual(result["dismissals_pending_review"], 1)

    def test_migration_timezone_offsets_preserve_the_deadline(self):
        self._seed_alert("offset", "alpha", enrichments={
            "dismissed": True, "dismissUntil": "2026-10-04T16:30:00+03:00",
        })
        with Session(self.engine) as session:
            SilencesMigrationBL(session).apply("keep")
            rule = session.exec(select(Silence)).one()
            self.assertEqual(rule.ends_at, datetime(2026, 10, 4, 13, 30))

    def test_stale_inventory_cannot_apply_or_spoof_a_team(self):
        self._seed_alert("stale", "alpha", enrichments={"dismissed": True})
        with Session(self.engine) as session:
            migration = SilencesMigrationBL(session)
            report = migration.inventory("keep")
            report["dismissals"]["items"][0]["detected_team_id"] = "beta"
            migration.apply("keep", inventory_report=report)
            self.assertEqual(session.exec(select(Silence)).one().team_id, "alpha")
        self._seed_alert("changed", "alpha", enrichments={"dismissed": True})
        with Session(self.engine) as session:
            migration = SilencesMigrationBL(session)
            report = migration.inventory("keep")
            row = session.exec(select(AlertEnrichment).where(AlertEnrichment.alert_fingerprint == "changed")).one()
            row.enrichments = {"dismissed": True, "dismissUntil": "forever", "note": "changed"}
            session.add(row)
            session.commit()
            with self.assertRaises(ValueError):
                migration.apply("keep", inventory_report=report)
            self.assertEqual(len(session.exec(select(Silence)).all()), 1)

    def test_maintenance_requires_explicit_teams_and_honours_ignored_statuses(self):
        from keep.api.bl.silences_evaluator import SilenceEvaluator
        a = self._seed_alert("maint-a", "alpha")
        b = self._seed_alert("maint-b", "beta")
        resolved = self._seed_alert("maint-resolved", "alpha", status="resolved")
        with Session(self.engine) as session:
            old = MaintenanceWindowRule(tenant_id="keep", name="Maintenance", cel_query="true",
                start_time=NOW - timedelta(minutes=10), end_time=NOW + timedelta(hours=2),
                created_by="legacy@example.test", ignore_statuses=["resolved"])
            session.add(old)
            session.commit()
            migration = SilencesMigrationBL(session)
            result = migration.apply("keep")
            self.assertEqual(result["maintenance_rules_migrated"], 0)
            self.assertEqual(result["maintenance_rules_pending_review"], 1)
            migration.apply("keep", maintenance_teams={"keep": {str(old.id): {"teams": ["alpha"], "cel": "true"}}})
            rows = [session.get(Alert, identifier) for identifier in (a, b, resolved)]
            effective = SilenceEvaluator(session, "keep", NOW).alerts(rows)
            self.assertTrue(effective[a].silenced)
            self.assertFalse(effective[b].silenced)
            self.assertFalse(effective[resolved].silenced)

    def test_maintenance_plan_requires_reviewed_cel_and_rejects_unknown_rules_atomically(self):
        from keep.api.bl.silences_evaluator import SilenceEvaluator
        alert_id = self._seed_alert("source-rule", "alpha", enrichments={"dismissed": 1})
        with Session(self.engine) as session:
            alert = session.get(Alert, alert_id)
            alert.event = {**alert.event, "source": ["test"]}
            session.add(alert)
            old = MaintenanceWindowRule(tenant_id="keep", name="Source", cel_query="source == 'test'",
                start_time=NOW - timedelta(minutes=10), end_time=NOW + timedelta(hours=2), created_by="old")
            session.add(old)
            session.commit()
            identifier = str(old.id)
            migration = SilencesMigrationBL(session)
            for plan in ({"keep": {identifier: ["alpha"]}},
                         {"keep": {"999999": {"teams": ["alpha"], "cel": "true"}}}):
                with self.assertRaises(ValueError):
                    migration.apply("keep", maintenance_teams=plan)
                self.assertEqual(len(session.exec(select(Silence)).all()), 0)
            result = migration.apply("keep", maintenance_teams={
                "keep": {identifier: {"teams": ["alpha"], "cel": "source[0] == 'test'"}},
            })
            self.assertEqual(result["dismissals_migrated"], 1)
            self.assertEqual(result["maintenance_rules_pending_review"], 0)
            self.assertEqual(result["admin_action_required"], [])
            imported = session.exec(select(Silence).where(Silence.correlation_id == f"legacy-maintenance:{identifier}")).one()
            self.assertEqual(imported.selector["cel"], "source[0] == 'test'")
            effective = SilenceEvaluator(session, "keep", NOW).alerts([session.get(Alert, alert_id)])[alert_id]
            self.assertTrue(any(reason.silence_id == imported.id for reason in effective.reasons))

    def test_cli_apply_requires_reviewed_inventory_and_rejects_stale_sources(self):
        import importlib.util
        import io
        import os
        import sys
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "lab/migrate-legacy-silences.py"
        spec = importlib.util.spec_from_file_location("silence_migration_cli_test", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(sys, "argv", [str(path), "--apply"]), \
             patch.object(sys, "stderr", io.StringIO()), \
             patch.object(cli, "get_session_sync") as get_session:
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 2)
            get_session.assert_not_called()

        self._seed_alert("cli", "alpha", enrichments={"dismissed": True})
        with Session(self.engine) as session:
            report = {"inventory": SilencesMigrationBL(session).inventory("keep")}
            enrichment = session.exec(select(AlertEnrichment)).one()
            enrichment.enrichments = {**enrichment.enrichments, "note": "Changed after review"}
            session.add(enrichment)
            session.commit()
        destination = Path(os.environ.get("TMPDIR", ".lab-work/silences-api")) / f"inventory-{uuid4().hex}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report))
        with patch.object(sys, "argv", [str(path), "--apply", "--tenant-id", "keep", "--reviewed-inventory", str(destination)]), \
             patch.object(sys, "stdout", io.StringIO()), \
             patch.object(cli, "get_session_sync", side_effect=lambda: Session(self.engine)):
            with self.assertRaisesRegex(ValueError, "source changed"):
                cli.main()
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Silence)).all()), 0)

    def test_legacy_restore_cancels_only_its_dedicated_rule_and_clears_raw_flags(self):
        from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
        alert_id = self._seed_alert("restore-owned", "alpha")
        client = self._create_test_client()
        headers = {"x-forwarded-email": "member@example.test", "x-forwarded-groups": "/roles/responder,/teams/alpha"}
        body = {"fingerprint": "restore-owned", "enrichments": {"dismissed": True}}
        for _ in range(2):
            response = client.post("/alerts/enrich", json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Silence)).all()), 1)
        response = client.post("/alerts/enrich", json={
            "fingerprint": "restore-owned", "enrichments": {"dismissed": False},
        }, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as session:
            self.assertIsNotNone(session.exec(select(Silence)).one().cancelled_at)
            dto = convert_db_alerts_to_dto_alerts([session.get(Alert, alert_id)], session=session)[0]
            self.assertFalse(dto.dismissed)
            self.assertFalse(dto.silence.silenced)

    def test_legacy_invalid_deadline_and_impersonation_write_nothing(self):
        self._seed_alert("unsafe", "alpha")
        client = self._create_test_client()
        headers = {"x-forwarded-email": "member@example.test", "x-forwarded-groups": "/roles/responder,/teams/alpha"}
        body = {"fingerprint": "unsafe", "enrichments": {"dismissed": True, "dismissUntil": "broken"}}
        self.assertEqual(client.post("/alerts/enrich", json=body, headers=headers).status_code, 422)
        body["enrichments"].pop("dismissUntil")
        response = client.post("/alerts/enrich", json=body,
            headers={**headers, "X-KEEP-USER": "admin@example.test", "X-KEEP-ROLE": "admin"})
        self.assertEqual(response.status_code, 403, response.text)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Silence)).all()), 0)

    def test_legacy_batch_rolls_back_a_failure_after_the_first_rule(self):
        self._seed_alert("atomic-a", "alpha")
        self._seed_alert("atomic-b", "alpha")
        original = SilencesBL.create
        calls = 0
        def fail_second(bl, command):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second command failed")
            return original(bl, command)
        with Session(self.engine) as session, patch.object(SilencesBL, "create", new=fail_second):
            with self.assertRaises(RuntimeError):
                alerts._handle_legacy_silence_compatibility(session, self.responder_alpha,
                    ["atomic-a", "atomic-b"], {"dismissed": True})
        with Session(self.engine) as session:
            for model in (Silence, SilenceEvent, SilenceCommand):
                self.assertEqual(len(session.exec(select(model)).all()), 0)
