"""Task 17 tests: Silence Gate, notification dispatch suppression, retry recheck, and missing step result handling."""

from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import MagicMock, patch
from sqlmodel import Session

from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident
from keep.api.models.db.incident import Incident
from keep.api.models.db.silence import Silence
from keep.api.models.silence import utc_string
from keep.contextmanager.contextmanager import ContextManager
from keep.step.step import Step, StepType
from tests.test_silences_api_fork import SilenceDatabaseCase, NOW


class MockProvider:
    def __init__(self, fail_count=0):
        self.notify_calls = []
        self.query_calls = []
        self.results = []
        self.fail_count = fail_count
        self.calls = 0

    def notify(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise Exception(f"Mock provider error on call {self.calls}")
        self.notify_calls.append(kwargs)
        res = {"status": "sent", "call_num": len(self.notify_calls), **kwargs}
        self.results.append(res)
        return res

    def query(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise Exception(f"Mock provider error on call {self.calls}")
        self.query_calls.append(kwargs)
        res = {"status": "queried", "call_num": len(self.query_calls), **kwargs}
        self.results.append(res)
        return res

    def expose(self):
        return {}


class SilenceNotificationDispatchTests(SilenceDatabaseCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("keep.api.bl.silences_evaluator.utc_now", return_value=NOW))
        self.context_manager = ContextManager(tenant_id="keep")

    def _create_rule(self, team_id, selector, starts_at=None, ends_at=None):
        rule_id = uuid4()
        with Session(self.engine) as session:
            rule = Silence(
                id=rule_id,
                tenant_id="keep",
                team_id=team_id,
                revision=1,
                selector=selector,
                starts_at=starts_at or (NOW - timedelta(hours=1)),
                ends_at=ends_at or (NOW + timedelta(hours=1)),
                comment="Test silence",
                created_by={"sub": "admin@example.test", "type": "user"},
                updated_by={"sub": "admin@example.test", "type": "user"},
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(hours=1),
                origin="ui",
                last_event_state="active",
            )
            session.add(rule)
            session.commit()
        return rule_id

    def test_notification_suppression_alert(self):
        """Active silence rule suppresses notification: true action."""
        rule_id = self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        self.context_manager.set_event_context({
            "fingerprint": "a",
            "team_id": "alpha",
            "name": "a",
            "status": "firing",
        })

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-step",
            config={"name": "notify-step", "notification": True},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"text": "Alert firing"},
            notification=True,
        )

        ran = step.run()
        self.assertFalse(ran)
        self.assertEqual(len(provider.notify_calls), 0)
        self.assertEqual(provider.results, [])

        ctx = self.context_manager.steps_context["notify-step"]
        self.assertTrue(ctx["skipped"])
        self.assertEqual(ctx["skip_reason"], "silenced")
        self.assertIsNone(ctx["results"])
        self.assertEqual(len(ctx["silence_reasons"]), 1)
        self.assertEqual(ctx["silence_reasons"][0]["silence_id"], str(rule_id))

    def test_non_notification_action_runs_even_if_silenced(self):
        """Active silence rule does not suppress notification: false or un-flagged action."""
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        self.context_manager.set_event_context({
            "fingerprint": "a",
            "team_id": "alpha",
            "name": "a",
            "status": "firing",
        })

        step = Step(
            context_manager=self.context_manager,
            step_id="ticket-step",
            config={"name": "ticket-step", "notification": False},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"text": "Create ticket"},
            notification=False,
        )

        ran = step.run()
        self.assertTrue(ran)
        self.assertEqual(len(provider.notify_calls), 1)
        self.assertIsNotNone(provider.results)
        self.assertEqual(self.context_manager.steps_context["ticket-step"]["results"]["status"], "sent")

    def test_alert_event_saved_status_not_changed_to_suppressed(self):
        """Ingested alert status in DB remains firing/resolved, never changed to suppressed."""
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        with Session(self.engine) as session:
            alert = session.get(Alert, self.alpha_id)
            self.assertEqual(alert.event["status"], "firing")
            self.assertNotEqual(alert.event["status"], "suppressed")

    def test_retry_rechecks_before_provider(self):
        """If a rule is created after first send fails, retry re-checks and suppresses provider call."""
        provider = MockProvider(fail_count=2)
        self.context_manager.set_event_context({
            "fingerprint": "a",
            "team_id": "alpha",
            "name": "a",
            "status": "firing",
        })

        created_rule = False

        # Custom notify that creates silence rule in DB when first call fails
        def notify_with_rule_creation(**kwargs):
            nonlocal created_rule
            if not created_rule:
                self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})
                created_rule = True
                raise Exception("First send failed before rule")
            return {"status": "sent"}

        provider.notify = notify_with_rule_creation

        step = Step(
            context_manager=self.context_manager,
            step_id="retry-notify",
            config={
                "name": "retry-notify",
                "notification": True,
                "on-failure": {"retry": {"count": 1, "interval": 0}},
            },
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"text": "Message"},
            notification=True,
        )

        ran = step.run()
        self.assertFalse(ran)
        self.assertEqual(provider.results, [])

        ctx = self.context_manager.steps_context["retry-notify"]
        self.assertTrue(ctx["skipped"])
        self.assertEqual(ctx["skip_reason"], "silenced")
        self.assertIsNone(ctx["results"])

    def test_missing_step_result_handling(self):
        """Subsequent step depending on a silenced step's result is skipped with missing_step_result."""
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        self.context_manager.set_event_context({
            "fingerprint": "a",
            "team_id": "alpha",
            "name": "a",
            "status": "firing",
        })

        # Step 1: Silenced notification
        provider1 = MockProvider()
        step1 = Step(
            context_manager=self.context_manager,
            step_id="step1",
            config={"name": "step1", "notification": True},
            step_type=StepType.ACTION,
            provider=provider1,
            provider_parameters={"text": "Notify"},
            notification=True,
        )
        ran1 = step1.run()
        self.assertFalse(ran1)
        self.assertEqual(len(provider1.notify_calls), 0)

        # Step 2: Depends on step1.results
        provider2 = MockProvider()
        step2 = Step(
            context_manager=self.context_manager,
            step_id="step2",
            config={
                "name": "step2",
                "notification": False,
                "alias": {"step1_call_num": "{{ steps.step1.results.call_num }}"},
            },
            step_type=StepType.ACTION,
            provider=provider2,
            provider_parameters={"call_num": "{{ steps.step1.results.call_num }}"},
            notification=False,
        )
        ran2 = step2.run()
        self.assertFalse(ran2)
        self.assertEqual(len(provider2.notify_calls), 0)

        ctx2 = self.context_manager.steps_context["step2"]
        self.assertTrue(ctx2["skipped"])
        self.assertEqual(ctx2["skip_reason"], "missing_step_result")
        self.assertEqual(ctx2["missing_from_step"], "step1")
        self.assertIsNone(ctx2["results"])

        # Step 3: Independent action runs normally
        provider3 = MockProvider()
        step3 = Step(
            context_manager=self.context_manager,
            step_id="step3",
            config={"name": "step3", "notification": False},
            step_type=StepType.ACTION,
            provider=provider3,
            provider_parameters={"text": "Independent action"},
            notification=False,
        )
        ran3 = step3.run()
        self.assertTrue(ran3)
        self.assertEqual(len(provider3.notify_calls), 1)
        self.assertEqual(self.context_manager.steps_context["step3"]["results"]["status"], "sent")

    def test_foreach_skips_silenced_and_sends_unsilenced(self):
        """Foreach over items sends un-silenced items and suppresses silenced ones."""
        # Silence 'a', but not 'b'
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        items = [
            {"fingerprint": "a", "team_id": "alpha", "name": "alert-a"},
            {"fingerprint": "b", "team_id": "beta", "name": "alert-b"},
        ]
        self.context_manager.set_step_context("get-items", results=items)

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-foreach",
            config={
                "name": "notify-foreach",
                "notification": True,
                "foreach": "{{ steps.get-items.results }}",
            },
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"target": "{{ foreach.value.name }}"},
            notification=True,
        )

        ran = step.run()
        self.assertTrue(ran)
        # Only 'b' was notified
        self.assertEqual(len(provider.notify_calls), 1)
        self.assertEqual(provider.notify_calls[0]["target"], "alert-b")
        context = self.context_manager.steps_context["notify-foreach"]
        self.assertFalse(context["skipped"])
        self.assertNotIn("skip_reason", context)
        self.assertEqual(context["skipped_items"][0]["index"], 0)
        self.assertEqual(context["skipped_items"][0]["skip_reason"], "silenced")
        self.assertEqual(len(context["results"]), 1)

    def test_foreach_storage_failure_records_skip_and_does_not_stop_automation(self):
        items = [{"fingerprint": "a"}, {"fingerprint": "b"}]
        self.context_manager.set_step_context("items", results=items)
        provider = MockProvider()
        step = Step(self.context_manager, "failed-foreach",
                    {"notification": True, "foreach": "{{ steps.items.results }}"},
                    StepType.ACTION, provider, {"text": "notify"}, notification=True)
        with patch.object(step, "_get_db_session", side_effect=RuntimeError("storage failure")):
            self.assertFalse(step.run())
        context = self.context_manager.steps_context["failed-foreach"]
        self.assertEqual(context["skip_reason"], "silence_verification_unavailable")
        self.assertIsNone(context["results"])
        self.assertEqual([item["index"] for item in context["skipped_items"]], [0, 1])
        self.assertEqual(provider.notify_calls, [])
        dependent = Step(self.context_manager, "dependent", {}, StepType.ACTION, MockProvider(),
                         {"text": "{{ actions.failed-foreach.results }}"})
        self.assertFalse(dependent.run())
        automation = Step(self.context_manager, "automation", {}, StepType.ACTION, MockProvider(),
                          {"text": "independent"})
        self.assertTrue(automation.run())

    def test_foreach_all_silenced(self):
        """Foreach where all items are silenced marks the step as silenced with results=None."""
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})
        self._create_rule("beta", {"kind": "alert", "fingerprints": ["b"]})

        provider = MockProvider()
        items = [
            {"fingerprint": "a", "team_id": "alpha", "name": "alert-a"},
            {"fingerprint": "b", "team_id": "beta", "name": "alert-b"},
        ]
        self.context_manager.set_step_context("get-items", results=items)

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-foreach-all",
            config={
                "name": "notify-foreach-all",
                "notification": True,
                "foreach": "{{ steps.get-items.results }}",
            },
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"target": "{{ foreach.value.name }}"},
            notification=True,
        )

        ran = step.run()
        self.assertFalse(ran)
        self.assertEqual(len(provider.notify_calls), 0)
        self.assertEqual(provider.results, [])

        ctx = self.context_manager.steps_context["notify-foreach-all"]
        self.assertTrue(ctx["skipped"])
        self.assertEqual(ctx["skip_reason"], "silenced")
        self.assertIsNone(ctx["results"])

    def test_real_base_provider_remains_usable_after_skip_and_foreach_reuse(self):
        from keep.providers.base.base_provider import BaseProvider

        rule_id = self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})
        provider = MagicMock(spec=BaseProvider)
        provider.results = []
        provider._notify.return_value = {"sent": True}
        provider.notify.side_effect = lambda **params: BaseProvider.notify(provider, **params)
        provider.expose.return_value = {}
        self.context_manager.set_event_context({"fingerprint": "a"})
        blocked = Step(self.context_manager, "blocked", {"notification": True},
                       StepType.ACTION, provider, {"text": "blocked"}, notification=True)
        self.assertFalse(blocked.run())
        self.assertEqual(provider.results, [])
        independent = Step(self.context_manager, "independent", {}, StepType.ACTION, provider,
                           {"text": "automation"})
        self.assertTrue(independent.run())
        self.assertEqual(provider.results, [{"sent": True}])

        self.context_manager.set_step_context("items", results=[{"fingerprint": "a"}])
        foreach = Step(self.context_manager, "reused", {"foreach": "{{ steps.items.results }}"},
                       StepType.ACTION, provider, {"text": "foreach"}, notification=True)
        self.assertFalse(foreach.run())
        self.assertEqual(provider.results, [{"sent": True}])
        with Session(self.engine) as session:
            rule = session.get(Silence, rule_id)
            rule.ends_at = NOW
            session.add(rule)
            session.commit()
        self.assertTrue(foreach.run())
        context = self.context_manager.steps_context["reused"]
        self.assertFalse(context["skipped"])
        self.assertNotIn("skip_reason", context)
        self.assertNotIn("skipped_items", context)
        self.assertEqual(context["results"], [{"sent": True}])
        provider._notify.assert_called_with(text="foreach")
        self.assertEqual(provider._notify.call_count, 2)
        self.assertTrue(blocked.run())
        self.assertFalse(self.context_manager.steps_context["blocked"]["skipped"])
        self.assertNotIn("skip_reason", self.context_manager.steps_context["blocked"])

    def test_incident_full_coverage_silenced(self):
        """Incident fully covered by explicit silence rule suppresses notification action."""
        inc_id = uuid4()
        with Session(self.engine) as session:
            inc = Incident(
                id=inc_id,
                tenant_id="keep",
                team_id="alpha",
                status="firing",
                user_generated_name="Disk incident",
            )
            session.add(inc)
            session.commit()

        self._create_rule("alpha", {"kind": "incident", "incident_ids": [str(inc_id)]})

        provider = MockProvider()
        self.context_manager.set_incident_context({
            "id": inc_id,
            "team_id": "alpha",
            "name": "Disk incident",
            "alerts": [],
        })

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-incident",
            config={"name": "notify-incident", "notification": True},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"title": "Incident firing"},
            notification=True,
        )

        ran = step.run()
        self.assertFalse(ran)
        self.assertEqual(len(provider.notify_calls), 0)
        ctx = self.context_manager.steps_context["notify-incident"]
        self.assertTrue(ctx["skipped"])
        self.assertEqual(ctx["skip_reason"], "silenced")
        self.assertIsNone(ctx["results"])

    def test_incident_partial_coverage_safe_content(self):
        """An aggregate payload must not leak silenced data or mutate later automation."""
        # Incident has two alerts: 'a' (alpha) and 'a2' (alpha)
        a2_id = self.alert("a2", "alpha")
        inc_id = uuid4()
        with Session(self.engine) as session:
            inc = Incident(
                id=inc_id,
                tenant_id="keep",
                team_id="alpha",
                status="firing",
                user_generated_name="Mixed incident",
            )
            session.add(inc)
            session.add(LastAlertToIncident(
                incident_id=inc_id, fingerprint="a", tenant_id="keep",
            ))
            session.add(LastAlertToIncident(
                incident_id=inc_id, fingerprint="a2", tenant_id="keep",
            ))
            session.commit()

        # Silence rule only covers 'a'
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        incident_data = {
            "id": inc_id,
            "team_id": "alpha",
            "name": "Mixed incident",
            "alerts": [
                {"fingerprint": "a", "name": "disk-1"},
                {"fingerprint": "a2", "name": "cpu-1"},
            ],
            "alerts_count": 2,
        }
        self.context_manager.set_incident_context(incident_data)

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-mixed-incident",
            config={"name": "notify-mixed-incident", "notification": True},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"title": "disk-1 and cpu-1", "incident_id": str(inc_id)},
            notification=True,
        )

        ran = step.run()
        self.assertFalse(ran)
        self.assertEqual(len(provider.notify_calls), 0)
        self.assertEqual(
            self.context_manager.steps_context["notify-mixed-incident"]["skip_reason"],
            "silence_partial_payload_unsafe",
        )

        remaining_alerts = self.context_manager.incident_context["alerts"]
        self.assertEqual([a["fingerprint"] for a in remaining_alerts], ["a", "a2"])
        self.assertEqual(self.context_manager.incident_context["alerts_count"], 2)
        automation = Step(
            self.context_manager, "automation", {"name": "automation"},
            StepType.ACTION, MockProvider(), {"count": "{{ incident.alerts_count }}"},
        )
        self.assertTrue(automation.run())
        self.assertEqual(automation.provider.notify_calls[0]["count"], "2")

    def test_service_lifecycle_events_bypass_silence(self):
        """Service lifecycle events (silence.created, etc.) bypass the silence gate."""
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        self.context_manager.set_event_context({
            "event_type": "silence.created",
            "silence_id": str(uuid4()),
            "team_id": "alpha",
        })

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-lifecycle",
            config={"name": "notify-lifecycle", "notification": True},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"text": "Silence event occurred"},
            notification=True,
        )

        ran = step.run()
        self.assertTrue(ran)
        self.assertEqual(len(provider.notify_calls), 1)
        self.assertFalse(self.context_manager.steps_context["notify-lifecycle"].get("skipped", False))

    def test_alert_name_cannot_bypass_the_gate(self):
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})
        self.context_manager.set_event_context({"fingerprint": "a", "name": "silence.created"})
        step = Step(self.context_manager, "notify", {"notification": True},
                    StepType.ACTION, MockProvider(), {"text": "secret"})
        self.assertFalse(step.run())
        self.assertEqual(step.provider.notify_calls, [])

    def test_historical_event_uses_its_own_status(self):
        self._create_rule("alpha", {"kind": "filter", "cel": "status == 'firing'"})
        self.alert("a", "alpha", status="resolved")
        self.context_manager.set_event_context({
            "event_id": str(self.alpha_id), "fingerprint": "a", "status": "firing",
        })
        step = Step(self.context_manager, "notify", {"notification": True},
                    StepType.ACTION, MockProvider(), {"text": "old firing"})
        self.assertFalse(step.run())
        self.assertEqual(step.provider.notify_calls, [])

    def test_unverified_target_fails_closed_and_automation_continues(self):
        self._create_rule("alpha", {"kind": "filter", "cel": "true"})
        self.context_manager.set_event_context({"fingerprint": "unknown", "team_id": "other"})
        step = Step(self.context_manager, "notify", {"notification": True},
                    StepType.ACTION, MockProvider(), {"text": "unverified"})
        self.assertFalse(step.run())
        self.assertEqual(step.provider.notify_calls, [])
        self.assertEqual(self.context_manager.steps_context["notify"]["skip_reason"],
                         "silence_verification_unavailable")
        automation = Step(self.context_manager, "ticket", {}, StepType.ACTION,
                          MockProvider(), {"text": "Create ticket"})
        self.assertTrue(automation.run())

    def test_storage_failure_is_not_reported_as_silence(self):
        self.context_manager.set_event_context({"fingerprint": "a"})
        step = Step(self.context_manager, "notify", {"notification": True},
                    StepType.ACTION, MockProvider(), {})
        with patch.object(step, "_get_db_session", side_effect=RuntimeError("DB unavailable")):
            self.assertFalse(step.run())
        self.assertEqual(step.provider.notify_calls, [])
        self.assertEqual(self.context_manager.steps_context["notify"]["skip_reason"],
                         "silence_verification_unavailable")

    def test_actions_alias_dependency_is_skipped(self):
        self.context_manager.steps_context["notify"] = {
            "skipped": True, "skip_reason": "silenced", "results": None,
        }
        step = Step(self.context_manager, "dependent", {}, StepType.ACTION,
                    MockProvider(), {"text": "{{ actions.notify.results.id }}"})
        self.assertFalse(step.run())
        self.assertEqual(step.provider.notify_calls, [])
        self.assertEqual(self.context_manager.steps_context["dependent"]["skip_reason"],
                         "missing_step_result")

    def test_reading_skip_metadata_does_not_skip_independent_action(self):
        self.context_manager.steps_context["notify"] = {
            "skipped": True, "skip_reason": "silenced", "results": None,
        }
        step = Step(self.context_manager, "audit", {}, StepType.ACTION,
                    MockProvider(), {"text": "{{ steps.notify.skip_reason }}"})
        self.assertTrue(step.run())
        self.assertEqual(step.provider.notify_calls[0]["text"], "silenced")

    def test_parser_extracts_notification_flag(self):
        """Parser extracts notification: true/false/None from action definition."""
        from unittest.mock import patch
        from keep.parser.parser import Parser
        parser = Parser()

        with patch("keep.parser.parser.ProvidersFactory.get_provider", return_value=MockProvider()):
            action_true = parser._get_action(
                self.context_manager,
                {"name": "act1", "notification": True, "provider": {"type": "mock"}},
            )
            self.assertTrue(action_true.notification)

            action_false = parser._get_action(
                self.context_manager,
                {"name": "act2", "notification": False, "provider": {"type": "mock"}},
            )
            self.assertFalse(action_false.notification)

            action_none = parser._get_action(
                self.context_manager,
                {"name": "act3", "provider": {"type": "mock"}},
            )
            self.assertIsNone(action_none.notification)

            action_str = parser._get_action(
                self.context_manager,
                {"name": "act4", "notification": "true", "provider": {"type": "mock"}},
            )
            self.assertTrue(action_str.notification)

    def test_canonical_data_used_for_evaluation(self):
        """Even if context_manager has spoofed team_id in external payload, canonical DB record determines silence match."""
        # Alert 'a' in DB belongs to team 'alpha'
        # Rule in DB covers team 'alpha'
        self._create_rule("alpha", {"kind": "alert", "fingerprints": ["a"]})

        provider = MockProvider()
        # External payload attempts to claim team_id="other" to bypass silence
        self.context_manager.set_event_context({
            "fingerprint": "a",
            "team_id": "other",  # spoofed team
            "name": "a",
            "status": "firing",
        })

        step = Step(
            context_manager=self.context_manager,
            step_id="notify-spoofed",
            config={"name": "notify-spoofed", "notification": True},
            step_type=StepType.ACTION,
            provider=provider,
            provider_parameters={"text": "Message"},
            notification=True,
        )

        ran = step.run()
        # Canonical alert 'a' in DB has team_id='alpha', which matches the silence rule!
        self.assertFalse(ran)
        self.assertEqual(len(provider.notify_calls), 0)
        self.assertTrue(self.context_manager.steps_context["notify-spoofed"]["skipped"])
        self.assertEqual(self.context_manager.steps_context["notify-spoofed"]["skip_reason"], "silenced")
