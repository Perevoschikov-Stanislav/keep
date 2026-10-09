"""Reviewed legacy cutover: identity, operator state, no side effects and replay."""

import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlmodel import Session, select

from keep.api.bl.legacy_incident_migration import LegacyIncidentMigration, export_snapshot, read_bridge_snapshot
from keep.api.models.db.alert import AlertAudit, AlertEnrichment, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.incident_correlation import IncidentCorrelationGroup
from keep.api.models.db.incident_migration import LegacyIncidentImport
from keep.api.models.db.incident_notification import IncidentNotificationBinding
from keep.api.models.db.rule import Rule
from keep.api.models.db.silence import NotificationDelivery, Silence, SilenceEvent
from tests.test_incident_notifications_fork import NotificationCase


class LegacyMigrationCase(NotificationCase):
    def setUp(self):
        super().setUp()
        self.migration = LegacyIncidentMigration("tenant")
        self.legacy_rule_id = uuid4()
        with Session(self.engine) as session:
            session.add(Rule(id=self.legacy_rule_id, tenant_id="tenant", name="Legacy fixture",
                definition={}, definition_cel="true", timeframe=600, created_by="fixture", creation_time=self.now(0)))
            session.commit()
        self.states = {}
        self.pause()

    def pause(self):
        self.bundle["runtime_ownership"] = [{"team_id": team, "domain": "disabled", "notifications": "disabled", "legacy_snooze": "disabled"}
                                             for team in ("alpha", "beta", None)]
        self.apply()

    def legacy(self, *, team="alpha", workload="catalog", status="firing", fingerprint=None, root=None):
        event = self.save(self.event(fingerprint or uuid4().hex, team=team, workload=workload))
        with Session(self.engine) as session:
            incident = Incident(tenant_id="tenant", team_id=team, rule_id=self.legacy_rule_id,
                rule_fingerprint="same-untrusted-fingerprint", incident_type="rule", status=status,
                assignee="oncall@example.test", user_generated_name="Manual title", user_summary="Manual notes",
                creation_time=self.now(0), start_time=self.now(0), end_time=self.now(1) if status == "resolved" else None,
                alerts_count=1)
            session.add(incident)
            session.flush()
            session.add(LastAlertToIncident(tenant_id="tenant", incident_id=incident.id, fingerprint=event.fingerprint))
            session.add(AlertEnrichment(tenant_id="tenant", alert_fingerprint=str(incident.id),
                enrichments={"ticket_url": "https://tickets.example.test/OPS-123"}))
            session.add(AlertAudit(tenant_id="tenant", fingerprint=str(incident.id), user_id="operator@example.test",
                timestamp=self.now(0), action="Incident status changed", description="Incident status changed from firing to acknowledged; manual"))
            session.commit()
            identifier = str(incident.id)
        if root:
            state = self.states[root]
            state["mm_members"] = json.dumps(json.loads(state["mm_members"]) + [identifier])
            self.states[identifier] = {"mm_root": root, "mm_key": "same-untrusted-fingerprint"}
        else:
            self.states[identifier] = {"mm_members": json.dumps([identifier]), "mm_post_id": "p" * 26,
                "mm_channel": team + "-channel", "mm_zone": team.upper(), "mm_key": "same-untrusted-fingerprint",
                "mm_status": status, "mm_flaps": 99, "mm_escalated": 4, "mm_resolved_at": self.now(1).isoformat()}
        return identifier

    def export(self):
        path = self.directory / "state.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS state (incident TEXT, key TEXT, value TEXT, PRIMARY KEY (incident,key))")
            connection.execute("DELETE FROM state")
            for identifier, state in self.states.items():
                connection.executemany("INSERT INTO state VALUES (?, ?, ?)", [(identifier, key, json.dumps(value)) for key, value in state.items()])
        with Session(self.engine) as session:
            return export_snapshot(session, "tenant", "bridge-fixture", read_bridge_snapshot(path))

    def manifest(self, exported, *, roots=None):
        return {"api_version": "keep.incidents/v1", "kind": "LegacyIncidentMigration", "id": "migration-fixture",
            "tenant_id": "tenant", "source_id": "bridge-fixture", "source_digest": exported["digest"],
            "candidate_digest": self.candidate().digest, "zones": {"ALPHA": "alpha", "BETA": "beta"},
            "rules": [{"legacy_rule_id": str(self.legacy_rule_id), "correlation_ref": "workload"}],
            "destinations": [{"legacy_id": team + "-channel", "destination_ref": team + "-chat"} for team in ("alpha", "beta")],
            "roots": roots or [{"root_id": root, "incident_id": root, "team_id": exported["incidents"][root]["data"]["incident"]["team_id"]}
                               for root in exported["roots"] if exported["incidents"].get(root)]}

    def preview(self, exported, plan):
        with Session(self.engine) as session:
            return self.migration.preview(session, exported, self.candidate(), plan, now=self.now(2))

    def migrate(self, exported, plan, preview=None):
        preview = preview or self.preview(exported, plan)
        with Session(self.engine) as session:
            return self.migration.apply(session, exported, self.candidate(), plan,
                expected_preview_digest=preview["preview_digest"], now=self.now(2))

    def rows(self, model):
        with Session(self.engine) as session:
            return session.exec(select(model)).all()

    def adopted_post(self, plan, root):
        plan["roots"][0].update(post_policy="adopt", verified_post={"external_id": self.states[root]["mm_post_id"],
            "destination_external_id": "alpha-channel", "exists": True,
            "checked_at": self.now(2).isoformat() + "Z", "source": "read_only_transport_export"})


class LegacyIncidentMigrationTest(LegacyMigrationCase):
    def test_dry_run_is_read_only_and_ignores_untrusted_legacy_keys(self):
        root = self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        before = self.rows(Incident)[0].dict()
        with patch("requests.request", side_effect=AssertionError("No transport calls")):
            report = self.preview(exported, plan)
        self.assertEqual(report["summary"]["ready"], 1)
        self.assertEqual(report["items"][0]["topology"], "one_to_one")
        self.assertNotEqual(report["items"][0]["group_ids"][0], "same-untrusted-fingerprint")
        self.assertEqual(before, self.rows(Incident)[0].dict())
        self.assertEqual(self.rows(LegacyIncidentImport), [])
        self.assertEqual(self.rows(NotificationDelivery), [])

    def test_import_preserves_manual_state_and_legacy_provenance_without_seeding_counters(self):
        root = self.legacy(status="acknowledged")
        exported = self.export()
        plan = self.manifest(exported)
        first = self.migrate(exported, plan)
        second = self.migrate(exported, plan, self.preview_from_receipt(first, exported, plan))
        self.assertEqual(second["result"], "noop")
        incident = self.rows(Incident)[0]
        self.assertEqual(str(incident.id), root)
        self.assertEqual((incident.status, incident.assignee, incident.user_generated_name, incident.user_summary),
                         ("acknowledged", "oncall@example.test", "Manual title", "Manual notes"))
        receipt = self.rows(LegacyIncidentImport)[0]
        self.assertEqual(receipt.provenance["bridge"]["state"]["mm_flaps"], 99)
        self.assertFalse(receipt.provenance["legacy_actor_verified"])
        self.assertFalse(receipt.provenance["history_complete"])
        self.assertEqual(len(receipt.provenance["domain_transitions"][root]), 1)
        self.assertEqual(incident.automation_context["cursors"], {})
        self.assertEqual(incident.automation_context["origin"], self.now(2).isoformat())
        self.assertEqual(self.rows(IncidentCorrelationGroup)[0].lifecycle_state["transitions"], [])
        self.assertEqual(len(self.rows(AlertAudit)), 2)
        self.assertEqual(self.rows(AlertEnrichment)[0].enrichments["ticket_url"], "https://tickets.example.test/OPS-123")
        self.assertEqual(self.rows(NotificationDelivery), [])

    def preview_from_receipt(self, first, exported, plan):
        return {"preview_digest": self.rows(LegacyIncidentImport)[0].provenance["reviewed_preview_digest"]}

    def test_split_is_explicit_and_never_merges_independent_new_groups(self):
        root = self.legacy()
        self.legacy(workload="billing", root=root)
        exported = self.export()
        plan = self.manifest(exported)
        report = self.preview(exported, plan)
        self.assertEqual(report["items"][0]["topology"], "many_to_many")
        self.assertIn("split_required", report["items"][0]["reasons"])
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.migrate(exported, plan, report)
        self.assertEqual(self.rows(LegacyIncidentImport), [])
        self.assertEqual(len(self.rows(Incident)), 2)

    def test_one_incident_with_two_workloads_requires_split_or_history_only(self):
        root = self.legacy()
        event = self.save(self.event("second-member", workload="billing"))
        with Session(self.engine) as session:
            session.add(LastAlertToIncident(tenant_id="tenant", incident_id=UUID(root), fingerprint=event.fingerprint))
            session.commit()
        exported = self.export()
        plan = self.manifest(exported)
        report = self.preview(exported, plan)
        self.assertEqual(report["items"][0]["topology"], "one_to_many")
        self.assertIn("split_required", report["items"][0]["reasons"])
        plan["roots"][0]["mode"] = "retain_history"
        self.migrate(exported, plan)
        self.assertIsNone(self.rows(Incident)[0].correlation_context)
        self.assertEqual(self.rows(IncidentCorrelationGroup), [])

    def test_many_to_one_requires_one_active_survivor_and_preserves_other_history(self):
        root = self.legacy()
        old = self.legacy(root=root, status="resolved")
        exported = self.export()
        plan = self.manifest(exported)
        report = self.preview(exported, plan)
        self.assertEqual(report["items"][0]["topology"], "many_to_one")
        self.assertEqual(report["items"][0]["status"], "ready")
        self.migrate(exported, plan)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Incident, UUID(old)).correlation_context)
        self.assertEqual(len(self.rows(Incident)), 2)

    def test_many_active_incidents_and_shared_post_cannot_be_adopted_as_one(self):
        root = self.legacy()
        self.legacy(root=root)
        exported = self.export()
        plan = self.manifest(exported)
        self.adopted_post(plan, root)
        report = self.preview(exported, plan)
        self.assertIn("multiple_active_incidents_require_review", report["items"][0]["reasons"])
        self.assertIn("post_scope_or_adapter_mismatch", report["items"][0]["reasons"])

    def test_foreign_team_and_foreign_link_history_are_rejected(self):
        root = self.legacy()
        self.legacy(team="beta", root=root)
        exported = self.export()
        plan = self.manifest(exported)
        self.assertIn("target_scope_mismatch", self.preview(exported, plan)["items"][0]["reasons"])
        with self.assertRaises(ValueError):
            self.migrate(exported, plan)

    def test_foreign_alert_history_blocks_even_history_only_import(self):
        root = self.legacy()
        event = self.save(self.event("foreign", team="beta"))
        with Session(self.engine) as session:
            session.add(LastAlertToIncident(tenant_id="tenant", incident_id=UUID(root), fingerprint=event.fingerprint))
            session.commit()
        exported = self.export()
        plan = self.manifest(exported)
        plan["roots"][0]["mode"] = "retain_history"
        self.assertIn("foreign_alert_history", self.preview(exported, plan)["items"][0]["reasons"])

    def test_missing_incident_and_history_are_reported_without_guessing(self):
        root = self.legacy()
        missing = str(uuid4())
        self.states[root]["mm_members"] = json.dumps([root, missing])
        exported = self.export()
        report = self.preview(exported, self.manifest(exported))
        self.assertEqual(report["items"][0]["status"], "unrecoverable")
        self.assertIn("missing_or_foreign_incident", report["items"][0]["reasons"])

    def test_missing_history_requires_explicit_metadata_retention(self):
        root = self.legacy()
        with Session(self.engine) as session:
            session.delete(session.exec(select(LastAlertToIncident)).one())
            session.commit()
        exported = self.export()
        plan = self.manifest(exported)
        self.assertIn("missing_history", self.preview(exported, plan)["items"][0]["reasons"])
        plan["roots"][0]["mode"] = "retain_history"
        self.assertEqual(self.preview(exported, plan)["items"][0]["status"], "ready")

    def test_verified_post_binding_is_restorable_and_import_does_not_send(self):
        root = self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        self.adopted_post(plan, root)
        self.migrate(exported, plan)
        binding = self.rows(IncidentNotificationBinding)[0]
        self.assertEqual(binding.external_id, "p" * 26)
        self.assertEqual(self.sent, [])
        with Session(self.engine) as session:
            saved = self.migration.receipts(session, "bridge-fixture")
            session.delete(session.get(IncidentNotificationBinding, binding.id))
            session.commit()
        with Session(self.engine) as session:
            result = self.migration.restore_bindings(session, "bridge-fixture", expected_snapshot_digest=saved["digest"])
            again = self.migration.restore_bindings(session, "bridge-fixture", expected_snapshot_digest=saved["digest"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(again["created"], 0)
        self.assertEqual(self.rows(IncidentNotificationBinding)[0].dict(), binding.dict())

    def test_lost_unverified_expired_and_wrong_destination_posts_have_explicit_plan(self):
        root = self.legacy()
        exported = self.export()
        original = self.manifest(exported)
        self.adopted_post(original, root)
        for change, expected in (({"exists": False}, "post_missing_or_unverified"),
                                 ({"destination_external_id": "beta-channel"}, "post_missing_or_unverified"),
                                 ({"checked_at": self.now(-4000).isoformat() + "Z"}, "post_receipt_expired")):
            plan = copy.deepcopy(original)
            plan["roots"][0]["verified_post"].update(change)
            self.assertIn(expected, self.preview(exported, plan)["items"][0]["reasons"])
        plan = self.manifest(exported)
        self.assertEqual(self.preview(exported, plan)["items"][0]["post_plan"], "retire_legacy_buttons_then_create")

    def test_snooze_becomes_one_team_scoped_incident_silence_with_same_end(self):
        root = self.legacy()
        self.states[root].update(snooze_until=self.now(100).isoformat(), snooze_by="unverified-mm-display-name")
        exported = self.export()
        plan = self.manifest(exported)
        self.assertIn("active_legacy_snooze_requires_import", self.preview(exported, plan)["items"][0]["reasons"])
        plan["roots"][0]["snooze"] = "import"
        preview = self.preview(exported, plan)
        self.migrate(exported, plan, preview)
        self.migrate(exported, plan, preview)
        silence = self.rows(Silence)[0]
        self.assertEqual(silence.team_id, "alpha")
        self.assertEqual(silence.selector, {"kind": "incident", "incident_ids": [root]})
        self.assertEqual(silence.ends_at.replace(tzinfo=None), self.now(100))
        self.assertEqual(silence.created_by["kind"], "service")
        self.assertEqual(len(self.rows(SilenceEvent)), 1)
        self.assertEqual(self.rows(NotificationDelivery), [])
        from keep.api.bl.silences_evaluator import SilenceEvaluator
        with Session(self.engine) as session:
            incident = session.get(Incident, UUID(root))
            self.assertTrue(SilenceEvaluator(session, "tenant", now=self.now(3)).incidents([incident])[incident.id].silenced)
            self.assertFalse(SilenceEvaluator(session, "tenant", now=self.now(100)).incidents([incident])[incident.id].silenced)

    def test_expired_snooze_does_not_create_a_new_silence(self):
        root = self.legacy()
        self.states[root]["snooze_until"] = self.now(1).isoformat()
        exported = self.export()
        plan = self.manifest(exported)
        plan["roots"][0]["snooze"] = "import"
        self.migrate(exported, plan)
        self.assertEqual(self.rows(Silence), [])

    def test_manual_change_after_preview_rejects_import_without_overwrite(self):
        root = self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        preview = self.preview(exported, plan)
        with Session(self.engine) as session:
            incident = session.get(Incident, UUID(root))
            incident.assignee = "new-owner@example.test"
            session.add(incident)
            session.commit()
        with self.assertRaisesRegex(ValueError, "operator/history state changed"):
            self.migrate(exported, plan, preview)
        self.assertEqual(self.rows(Incident)[0].assignee, "new-owner@example.test")
        self.assertEqual(self.rows(LegacyIncidentImport), [])

    def test_import_requires_explicit_paused_owners(self):
        self.legacy()
        self.bundle["runtime_ownership"] = []
        self.apply()
        exported = self.export()
        plan = self.manifest(exported)
        with self.assertRaisesRegex(ValueError, "paused"):
            self.migrate(exported, plan)

    def test_two_roots_with_same_new_group_do_not_overwrite_each_other(self):
        self.legacy()
        self.legacy()
        exported = self.export()
        report = self.preview(exported, self.manifest(exported))
        self.assertTrue(all("planned_group_collision" in item["reasons"] for item in report["items"]))

    def test_crash_rolls_back_entire_batch_and_resume_creates_one_set(self):
        self.legacy()
        self.legacy(workload="billing")
        exported = self.export()
        plan = self.manifest(exported)
        preview = self.preview(exported, plan)
        original = self.migration._import
        calls = []
        def crash(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("simulated power loss before commit")
            return original(*args)
        with patch.object(self.migration, "_import", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "power loss"):
                self.migrate(exported, plan, preview)
        self.assertEqual(self.rows(LegacyIncidentImport), [])
        self.assertEqual(self.rows(IncidentCorrelationGroup), [])
        self.assertTrue(all(incident.correlation_context is None for incident in self.rows(Incident)))
        self.migrate(exported, plan, preview)
        self.assertEqual(len(self.rows(LegacyIncidentImport)), 2)

    def test_replay_with_different_source_or_plan_is_rejected(self):
        self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        preview = self.preview(exported, plan)
        self.migrate(exported, plan, preview)
        changed = copy.deepcopy(plan)
        changed["id"] = "different-migration"
        with self.assertRaisesRegex(ValueError, "already imported"):
            self.migrate(exported, changed, preview)
        self.assertEqual(len(self.rows(LegacyIncidentImport)), 1)

    def test_imported_group_is_used_by_next_ingestion_after_cutover(self):
        root = self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        self.migrate(exported, plan)
        self.bundle["runtime_ownership"] = []
        self.apply()
        self.correlate(self.event("new-replica"), 3)
        self.assertEqual(len(self.rows(Incident)), 1)
        self.assertEqual(str(self.rows(Incident)[0].id), root)

    def test_bridge_reader_rejects_live_wal_and_leaves_source_unchanged(self):
        self.legacy()
        self.export()
        path = self.directory / "state.db"
        before = path.read_bytes()
        read_bridge_snapshot(path)
        self.assertEqual(path.read_bytes(), before)
        path.with_name("state.db-wal").write_bytes(b"fixture")
        with self.assertRaisesRegex(ValueError, "standalone"):
            read_bridge_snapshot(path)

    def test_preview_uses_canonical_enrichments_when_computing_new_identity(self):
        root = self.legacy(fingerprint="enriched")
        with Session(self.engine) as session:
            session.add(AlertEnrichment(tenant_id="tenant", alert_fingerprint="enriched",
                enrichments={"labels": {"cluster": "lab", "namespace": "ns", "kind": "workload", "workload": "billing"}}))
            session.commit()
        exported = self.export()
        report = self.preview(exported, self.manifest(exported))
        group = next(iter(report["items"][0]["groups"].values()))
        self.assertIn(["normalized.workload", ["string", "billing"]], group["key"]["values"])

    def test_missing_root_and_unmapped_zone_are_explicit(self):
        root = self.legacy()
        self.states[root]["mm_root"] = str(uuid4())
        exported = self.export()
        self.assertEqual(self.preview(exported, self.manifest(exported))["items"][0]["status"], "unrecoverable")
        self.states[root].pop("mm_root")
        self.states[root]["mm_zone"] = "UNMAPPED"
        exported = self.export()
        self.assertIn("unmapped_or_foreign_zone", self.preview(exported, self.manifest(exported))["items"][0]["reasons"])

    def test_changed_export_and_foreign_candidate_digest_are_rejected(self):
        self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        forged = copy.deepcopy(exported)
        forged["bridge_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "export digest"):
            self.preview(forged, plan)
        plan["candidate_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.preview(exported, plan)

    def test_cli_export_preview_import_and_replay_write_private_artifacts_only(self):
        import yaml
        from keep.api.core.legacy_incident_migration_cli import main
        self.legacy()
        self.export()
        (self.directory / "bundle.yaml").write_text(yaml.safe_dump(self.bundle))
        out = lambda name: str(self.directory / (name + ".json"))
        base = ["--tenant", "tenant"]
        export_args = ["export", *base, "--source-id", "bridge-fixture", "--state-db", str(self.directory / "state.db"), "--output", out("export-cli")]
        self.assertEqual(main(export_args), 0)
        exported = json.loads(Path(out("export-cli")).read_text())
        plan = self.manifest(exported)
        (self.directory / "plan.yaml").write_text(yaml.safe_dump(plan))
        args = [*base, "--export", out("export-cli"), "--bundle", str(self.directory / "bundle.yaml"), "--plan", str(self.directory / "plan.yaml")]
        self.assertEqual(main(["preview", *args, "--output", out("preview-cli")]), 0)
        apply_args = ["apply", *args, "--reviewed-preview", out("preview-cli"), "--state-db", str(self.directory / "state.db")]
        self.assertEqual(main([*apply_args, "--output", out("apply-cli")]), 0)
        self.assertEqual(main([*apply_args, "--output", out("replay-cli")]), 0)
        self.assertEqual(json.loads(Path(out("replay-cli")).read_text())["result"], "noop")
        self.assertEqual(Path(out("export-cli")).stat().st_mode & 0o777, 0o600)
        with patch("sys.stderr"):
            self.assertEqual(main(export_args), 1)  # Never replace a reviewed artifact.

    def test_different_arbitrary_teams_and_transports_validate_from_iac_examples(self):
        import yaml
        from keep.api.bl.incident_provisioning import Candidate
        from keep.api.core.incident_contract import validate_shape
        directory = Path(__file__).resolve().parents[1] / "config/incident-migration.example"
        candidates = [Candidate.from_file(directory / name / "bundle.yaml", "keep") for name in ("a", "b")]
        first, second = [{item["id"] for item in candidate.documents["access"]["teams"]} for candidate in candidates]
        self.assertTrue(first.isdisjoint(second))
        for name, candidate in zip(("a", "b"), candidates):
            validate_shape("LegacyIncidentMigration", yaml.safe_load((directory / name / "migration.yaml").read_text()), "migration")
            self.assertTrue(all(item["domain"] == item["notifications"] == "disabled" for item in candidate.bundle["runtime_ownership"]))
        self.assertEqual({item["kind"] for item in candidates[0].bundle["transports"]}, {"http_json"})
        self.assertIn("mattermost", {item["kind"] for item in candidates[1].bundle["transports"]})

    def test_restore_never_overwrites_a_live_or_reassigned_binding(self):
        root = self.legacy()
        exported = self.export()
        plan = self.manifest(exported)
        self.adopted_post(plan, root)
        self.migrate(exported, plan)
        with Session(self.engine) as session:
            saved = self.migration.receipts(session, "bridge-fixture")
            binding = session.exec(select(IncidentNotificationBinding)).one()
            binding.external_id = "new-post-id"
            session.add(binding)
            session.commit()
        with Session(self.engine) as session:
            with self.assertRaisesRegex(ValueError, "never overwrite"):
                self.migration.restore_bindings(session, "bridge-fixture", expected_snapshot_digest=saved["digest"])


class IncidentRuntimeOwnershipTest(NotificationCase):
    def test_schema_rejects_duplicate_unknown_and_competing_owners(self):
        for value in (
            [{"team_id": "foreign", "domain": "keep", "notifications": "keep", "legacy_snooze": "disabled"}],
            [{"team_id": "alpha", "domain": "legacy", "notifications": "keep", "legacy_snooze": "legacy"}],
            [{"team_id": "alpha", "domain": "keep", "notifications": "keep", "legacy_snooze": "legacy"}],
            [{"team_id": "alpha", "domain": "keep", "notifications": "keep", "legacy_snooze": "disabled"}] * 2):
            self.bundle["runtime_ownership"] = value
            with self.assertRaises(ValueError):
                self.candidate()

    def test_paused_team_does_not_correlate_but_other_team_continues(self):
        self.bundle["runtime_ownership"] = [{"team_id": "alpha", "domain": "disabled", "notifications": "disabled", "legacy_snooze": "disabled"}]
        self.apply()
        self.assertEqual(self.correlate(self.event()), [])
        self.correlate(self.event("beta", team="beta"))
        self.assertEqual([incident.team_id for incident in self.incidents()], ["beta"])
        self.assertEqual(self.service.status()["runtime_ownership"][0]["domain"], "disabled")

    def test_notification_claim_rechecks_owner_before_sending_old_queue(self):
        self.correlate(self.event())
        worker = self.dispatcher()
        worker._scan()
        claimed = worker.claim()
        self.bundle["runtime_ownership"] = [{"team_id": "alpha", "domain": "keep", "notifications": "disabled", "legacy_snooze": "disabled"}]
        self.apply()
        self.assertFalse(worker.send_claimed(claimed))
        self.assertEqual(self.sent, [])
        self.assertTrue(any(row.last_error_code == "runtime_notifications_owned_by_disabled" for row in self.deliveries()))

    def test_paused_domain_does_not_create_or_claim_sla_jobs(self):
        self.correlate(self.event())
        self.tick(10)
        operation = self.operations()[0]
        self.bundle["runtime_ownership"] = [{"team_id": "alpha", "domain": "disabled", "notifications": "disabled", "legacy_snooze": "disabled"}]
        self.apply()
        self.assertIsNone(self.worker().claim(operation.id, self.now(10)))
        before = len(self.operations())
        self.tick(30)
        self.assertEqual(len(self.operations()), before)

    def test_legacy_domain_routes_only_its_events_to_classic_rules(self):
        from keep.rulesengine.rulesengine import RulesEngine
        self.bundle["runtime_ownership"] = [{"team_id": "alpha", "domain": "legacy", "notifications": "legacy", "legacy_snooze": "legacy"}]
        self.apply()
        with patch.object(RulesEngine, "_run_cel_rules", return_value=[]) as legacy:
            self.correlate(self.event())
        self.assertEqual(legacy.call_args[0][0][0].team_id, "alpha")
        self.assertEqual(self.incidents(), [])
