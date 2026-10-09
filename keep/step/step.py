import logging
import time
from enum import Enum

from keep.conditions.condition_factory import ConditionFactory
from keep.contextmanager.contextmanager import ContextManager
from keep.exceptions.action_error import ActionError
from keep.iohandler.iohandler import IOHandler
from keep.providers.base.base_provider import BaseProvider
from keep.step.step_provider_parameter import StepProviderParameter
from keep.throttles.throttle_factory import ThrottleFactory


class StepType(Enum):
    STEP = "step"
    ACTION = "action"


class Step:
    def __init__(
        self,
        context_manager,
        step_id: str,
        config: dict,
        step_type: StepType,
        provider: BaseProvider,
        provider_parameters: dict,
        notification: bool | None = None,
        db_session=None,
    ):
        self.config = config
        self.step_id = step_id
        self.step_type = step_type
        self.provider = provider
        self.provider_parameters: dict[str, str | StepProviderParameter] = (
            provider_parameters
        )
        self.db_session = db_session
        notification_val = (
            notification
            if notification is not None
            else self.config.get("notification")
        )
        if isinstance(notification_val, bool):
            self.notification: bool | None = notification_val
        elif isinstance(notification_val, str) and notification_val.lower() in {"true", "false"}:
            self.notification: bool | None = notification_val.lower() == "true"
        elif notification_val is not None:
            raise ValueError("notification must be a boolean")
        else:
            self.notification: bool | None = None
        # backward compatibility
        legacy_on_failure = self.config.get("provider", {}).get("on-failure", {})
        self.on_failure = self.config.get("on-failure", {}) or legacy_on_failure
        self.context_manager: ContextManager = context_manager
        self.io_handler = IOHandler(context_manager)
        self.conditions = self.config.get("condition", [])
        self.vars = self.config.get("vars", {})
        self.conditions_results = {}
        self.logger = logging.getLogger(__name__)
        self.__retry = self.on_failure.get("retry", {})
        self.__retry_count = self.__retry.get("count", 0)
        self.__retry_interval = self.__retry.get("interval", 0)
        self.__continue_to_next_step = self.config.get("continue", True)
        self.__continue_on_error = self.config.get("continue_on_error", False)

    def _get_db_session(self):
        if self.db_session:
            return self.db_session
        if (
            hasattr(self.context_manager, "db_session")
            and self.context_manager.db_session
        ):
            return self.context_manager.db_session
        from keep.api.core.db import get_session_sync

        return get_session_sync()

    def _check_missing_step_results(self) -> str | None:
        """
        Check if this step/action requires results of a step that was skipped (silenced or missing_step_result).
        Returns the step_id of the missing dependency, or None.
        """
        contexts = getattr(self.context_manager, "steps_context", None)
        if not isinstance(contexts, dict):
            return None

        skipped_steps = {
            step_id: info
            for step_id, info in contexts.items()
            if isinstance(info, dict)
            and info.get("skipped") is True
            and info.get("results") is None
            and info.get("skip_reason") in {
                "silenced", "missing_step_result", "silence_partial_payload_unsafe",
                "silence_verification_unavailable",
            }
        }
        if not skipped_steps:
            return None

        import re

        step_pattern = re.compile(
            r"(?:steps|actions)(?:\.([a-zA-Z0-9_\-]+)|\['([a-zA-Z0-9_\-]+)'\]|\[\"([a-zA-Z0-9_\-]+)\"\])"
            r"(?:\.results\b|\['results'\]|\[\"results\"\])"
        )

        def find_step_deps(obj):
            deps = set()
            if isinstance(obj, str):
                for match in step_pattern.finditer(obj):
                    name = match.group(1) or match.group(2) or match.group(3)
                    if name:
                        deps.add(name)
            elif isinstance(obj, dict):
                for v in obj.values():
                    deps.update(find_step_deps(v))
            elif isinstance(obj, list):
                for item in obj:
                    deps.update(find_step_deps(item))
            return deps

        configs_to_check = [
            self.config.get("if"),
            self.config.get("foreach"),
            self.conditions,
            self.vars,
            self.config.get("alias"),
            self.provider_parameters,
        ]
        all_deps = set()
        for cfg in configs_to_check:
            if cfg:
                all_deps.update(find_step_deps(cfg))

        for dep_step_id in all_deps:
            if dep_step_id in skipped_steps:
                return dep_step_id

        return None

    def _mark_missing_step_result(self, dep_step_id: str):
        self.logger.info(
            f"Step {self.step_id} skipped: missing_step_result from {dep_step_id}",
            extra={
                "step_id": self.step_id,
                "skipped": True,
                "skip_reason": "missing_step_result",
                "missing_from_step": dep_step_id,
            },
        )
        self.context_manager.set_step_context(
            self.step_id, results=None, foreach=False
        )
        if self.step_id in self.context_manager.steps_context:
            self.context_manager.steps_context[self.step_id]["skipped"] = True
            self.context_manager.steps_context[self.step_id]["skip_reason"] = (
                "missing_step_result"
            )
            self.context_manager.steps_context[self.step_id]["missing_from_step"] = (
                dep_step_id
            )
            self.context_manager.steps_context[self.step_id]["results"] = None
        return False

    def _mark_silenced(self, silence_reasons: list[dict], skip_reason="silenced"):
        normalized_reasons = []
        for r in silence_reasons:
            if isinstance(r, dict):
                r_copy = dict(r)
                if "silence_id" in r_copy:
                    r_copy["silence_id"] = str(r_copy["silence_id"])
                normalized_reasons.append(r_copy)
            else:
                normalized_reasons.append(r)
        silence_reasons = normalized_reasons

        reason_ids = [
            str(r.get("silence_id"))
            for r in silence_reasons
            if isinstance(r, dict) and r.get("silence_id")
        ]
        self.logger.info(
            f"Action {self.step_id} skipped: {skip_reason}, rules {reason_ids}",
            extra={
                "step_id": self.step_id,
                "skipped": True,
                "skip_reason": skip_reason,
                "silence_reasons": silence_reasons,
            },
        )
        if not self.foreach:
            self.context_manager.set_step_context(
                self.step_id, results=None, foreach=False
            )
            if self.step_id in self.context_manager.steps_context:
                self.context_manager.steps_context[self.step_id]["skipped"] = True
                self.context_manager.steps_context[self.step_id]["skip_reason"] = skip_reason
                self.context_manager.steps_context[self.step_id]["silence_reasons"] = (
                    silence_reasons
                )
                self.context_manager.steps_context[self.step_id]["results"] = None
        else:
            if self.step_id in self.context_manager.steps_context:
                self.context_manager.steps_context[self.step_id]["skipped"] = True
                self.context_manager.steps_context[self.step_id]["skip_reason"] = skip_reason
                self.context_manager.steps_context[self.step_id]["silence_reasons"] = (
                    silence_reasons
                )
        return False

    def _check_silenced(self, curr_retry_count: int = 0):
        """Evaluate a persisted target in a fresh snapshot before every send attempt."""
        if self.step_type != StepType.ACTION or self.notification is not True:
            return False, [], None

        def field(value, name):
            return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

        item = getattr(self.context_manager, "foreach_context", {}).get("value")
        event = getattr(self.context_manager, "event_context", None)
        incident = getattr(self.context_manager, "incident_context", None)
        if item is not None and field(item, "fingerprint"):
            target, kind = item, "alert"
        elif item is not None and field(item, "id") and (
            isinstance(item, dict) and "alerts" in item or hasattr(item, "alerts")
        ):
            target, kind = item, "incident"
        elif field(event, "fingerprint"):
            target, kind = event, "alert"
        elif field(incident, "id"):
            target, kind = incident, "incident"
        else:
            # Lifecycle delivery has no alert/incident target. Event names never grant a bypass.
            return False, [], None

        from uuid import UUID
        from sqlalchemy import and_
        from sqlmodel import select
        from keep.api.bl.silences_evaluator import SilenceEvaluator
        from keep.api.models.db.alert import Alert, LastAlert
        from keep.api.models.db.incident import Incident

        tenant_id = self.context_manager.tenant_id
        session = self._get_db_session()
        managed = self.db_session is None and getattr(self.context_manager, "db_session", None) is None
        try:
            evaluator = SilenceEvaluator(session, tenant_id)
            if kind == "alert":
                fingerprint = field(target, "fingerprint")
                event_id = field(target, "event_id")
                if isinstance(target, Alert):
                    event_id = target.id
                query = select(Alert).where(Alert.tenant_id == tenant_id, Alert.fingerprint == fingerprint)
                if event_id:
                    query = query.where(Alert.id == UUID(str(event_id)))
                else:
                    owners = session.exec(select(Alert.team_id).where(
                        Alert.tenant_id == tenant_id, Alert.fingerprint == fingerprint,
                    ).distinct()).all()
                    if len(owners) != 1:
                        raise ValueError("Notification target has no unambiguous canonical owner")
                    query = query.join(LastAlert, and_(
                        LastAlert.tenant_id == Alert.tenant_id, LastAlert.alert_id == Alert.id,
                    )).where(LastAlert.fingerprint == fingerprint)
                canonical = session.exec(query).first()
                if canonical is None:
                    raise ValueError("Notification alert was not found in Keep")
                result = evaluator.alerts([canonical])[canonical.id]
            else:
                canonical = session.exec(select(Incident).where(
                    Incident.tenant_id == tenant_id, Incident.id == UUID(str(field(target, "id"))),
                )).first()
                if canonical is None:
                    raise ValueError("Notification incident was not found in Keep")
                result = evaluator.incidents([canonical])[canonical.id]
                if result.coverage == "partial":
                    # Arbitrary templates and remote receivers can reread the whole incident.
                    # Until they can rebuild a safe projection, skip this aggregate send only.
                    return True, [reason.dict() for reason in result.reasons], "incident_partial"
            return result.silenced, [reason.dict() for reason in result.reasons], kind
        finally:
            if managed:
                session.close()

    def _notification_gate(self):
        try:
            blocked, reasons, kind = self._check_silenced()
        except Exception:
            self.logger.exception("Notification skipped: silence verification unavailable",
                                  extra={"step_id": self.step_id})
            return self._mark_silenced([], "silence_verification_unavailable")
        if blocked:
            reason = "silence_partial_payload_unsafe" if kind == "incident_partial" else "silenced"
            return self._mark_silenced(reasons, reason)
        return None

    @property
    def foreach(self):
        return self.config.get("foreach")

    @property
    def name(self):
        return self.step_id

    @property
    def continue_to_next_step(self):
        return self.__continue_to_next_step

    @property
    def continue_on_error(self):
        return self.__continue_on_error

    def _dont_render(self):
        # special case for Keep provider on _notify with "if" - it should render the parameters itself
        return self.step_type == StepType.ACTION and "KeepProvider" in str(
            self.provider.__class__
        )

    def run(self):
        from keep.api.core.incident_configuration import check_workflow_configuration
        check_workflow_configuration(self.context_manager, self.db_session)
        try:
            if self.config.get("foreach"):
                did_action_run = self._run_foreach()
            # special case for Keep provider on _notify with "if" - it should render the parameters itself
            elif self._dont_render():
                did_action_run = self._run_single(dont_render=True)
            else:
                did_action_run = self._run_single()
            return did_action_run
        except Exception as e:
            self.logger.warning(
                "Failed to run step %s with error %s",
                self.step_id,
                e,
                extra={
                    "step_id": self.step_id,
                },
                exc_info=True,
            )
            raise ActionError(e)

    def _check_throttling(self, action_name):
        throttling = self.config.get("throttle")
        # if there is no throttling, return
        if not throttling:
            return False

        throttling_type = throttling.get("type")
        throttling_config = throttling.get("with")
        throttle = ThrottleFactory.get_instance(
            self.context_manager, throttling_type, throttling_config
        )
        workflow_id = self.context_manager.get_workflow_id()
        event_id = self.context_manager.event_context.event_id
        return throttle.check_throttling(action_name, workflow_id, event_id)

    def _get_foreach_items(self) -> list | list[list]:
        """Get the items to iterate over, when using the `foreach` attribute (see foreach.md)"""
        # TODO: this should be part of iohandler?

        # the item holds the value we are going to iterate over
        # TODO: currently foreach will support only {{ a.b.c }} and not functions and other things (which make sense)
        foreach_split = self.config.get("foreach").split("&&")
        foreach_items = []
        for foreach in foreach_split:
            index = foreach.replace("{{", "").replace("}}", "").split(".")
            index = [i.strip() for i in index]
            items = self.context_manager.get_full_context()
            for i in index:
                if isinstance(items, dict):
                    items = items.get(i, {})
                else:
                    items = getattr(items, i, {})
            foreach_items.append(items)
        if not foreach_items:
            return []
        return len(foreach_items) == 1 and foreach_items[0] or zip(*foreach_items)

    def _run_foreach(self):
        """Evaluate the action for each item, when using the `foreach` attribute (see foreach.md)"""
        missing_dep = self._check_missing_step_results()
        if missing_dep:
            return self._mark_missing_step_result(missing_dep)

        items = self._get_foreach_items()
        any_action_run = False
        all_silenced = True if items else False
        silenced_reasons_all = []
        skip_reasons = []
        skipped_items = []
        previous_context = self.context_manager.steps_context.get(self.step_id)
        if isinstance(previous_context, dict):
            previous_context.pop("skipped_items", None)
        self.context_manager.set_foreach_items(items=items)
        for index, item in enumerate(items):
            self.context_manager.set_foreach_value(value=item)
            try:
                did_action_run = self._run_single()
                if did_action_run:
                    any_action_run = True
                    all_silenced = False
                else:
                    step_ctx = self.context_manager.steps_context.get(self.step_id, {})
                    if step_ctx.get("skipped") and step_ctx.get("skip_reason") in {
                        "silenced", "silence_verification_unavailable", "silence_partial_payload_unsafe",
                    }:
                        silenced_reasons_all.extend(step_ctx.get("silence_reasons", []))
                        skip_reasons.append(step_ctx["skip_reason"])
                        skipped_items.append({"index": index, "skip_reason": step_ctx["skip_reason"],
                                              "silence_reasons": step_ctx.get("silence_reasons", [])})
                    else:
                        all_silenced = False
            except Exception as e:
                self.logger.warning(
                    "Failed to run step %s with error %s",
                    self.step_id,
                    e,
                    extra={
                        "step_id": self.step_id,
                    },
                    exc_info=True,
                )
                all_silenced = False
                continue

        self.context_manager.reset_foreach_context()
        if items and all_silenced and not any_action_run:
            self.context_manager.set_step_context(
                self.step_id, results=None, foreach=False
            )
            if self.step_id in self.context_manager.steps_context:
                self.context_manager.steps_context[self.step_id]["skipped"] = True
                self.context_manager.steps_context[self.step_id]["skip_reason"] = (
                    "silence_verification_unavailable" if "silence_verification_unavailable" in skip_reasons
                    else "silence_partial_payload_unsafe" if "silence_partial_payload_unsafe" in skip_reasons
                    else "silenced"
                )
                self.context_manager.steps_context[self.step_id]["silence_reasons"] = (
                    silenced_reasons_all
                )
                self.context_manager.steps_context[self.step_id]["results"] = None
        elif any_action_run:
            if self.step_id in self.context_manager.steps_context:
                self.context_manager.steps_context[self.step_id]["skipped"] = False
                self.context_manager.steps_context[self.step_id].pop("skip_reason", None)
                self.context_manager.steps_context[self.step_id].pop("silence_reasons", None)
        if skipped_items:
            self.context_manager.steps_context[self.step_id]["skipped_items"] = skipped_items

        return any_action_run

    def _run_single(self, dont_render=False):
        # 0. Check missing step results first
        missing_dep = self._check_missing_step_results()
        if missing_dep:
            return self._mark_missing_step_result(missing_dep)

        # Initialize all conditions
        conditions = []

        # set the step's vars in the context so they're available for condition/if evaluation
        # note: aliases are intentionally NOT rendered/set here - they are only computed
        # once we know the step is actually going to run (see below), since rendering them
        # can fail/throw if the step (and its "if") is meant to be skipped (e.g. because they
        # reference results of another step that was itself skipped)
        self.context_manager.set_step_vars(
            self.step_id, _vars=self.vars, _aliases={}
        )

        for condition in self.conditions:
            condition_name = condition.get("name", None)

            if not condition_name:
                raise Exception("Condition must have a name")

            conditions.append(
                ConditionFactory.get_condition(
                    self.context_manager,
                    condition.get("type"),
                    condition_name,
                    condition,
                )
            )

        for condition in conditions:
            condition_compare_to = condition.get_compare_to()
            condition_compare_value = condition.get_compare_value()
            try:
                condition_result = condition.apply(
                    condition_compare_to, condition_compare_value
                )
            except Exception as e:
                missing_dep = self._check_missing_step_results()
                if missing_dep:
                    return self._mark_missing_step_result(missing_dep)
                self.logger.error(
                    "Failed to apply condition %s with error %s",
                    condition.condition_name,
                    e,
                    extra={
                        "step_id": self.step_id,
                    },
                )
                raise
            self.context_manager.set_condition_results(
                self.step_id,
                condition.condition_name,
                condition.condition_type,
                condition_compare_to,
                condition_compare_value,
                condition_result,
                condition_alias=condition.condition_alias,
                **condition.condition_context,
            )

        # Second, decide if need to run
        # after all conditions are applied, check if we need to run
        # there are 2 cases:
        # 1. a "if" block is supplied, then use it
        # 2. no "if" block is supplied, then use the AND between all conditions
        if self.config.get("if"):
            if_conf = self.config.get("if")
        else:
            # create a string of all conditions, separated by "and"
            if_conf = " and ".join(
                [f"{{{{ {condition.condition_alias} }}}} " for condition in conditions]
            )

        # Now check it
        if if_conf:
            try:
                quoted_if_conf = self.io_handler.quote(if_conf)
                if_met = self.io_handler.render(quoted_if_conf, safe=False)
                # Evaluate the condition string
                from asteval import Interpreter

                aeval = Interpreter()
                evaluated_if_met = aeval(if_met)
                # tb: when Shahar and I debugged, conclusion was:
                if isinstance(evaluated_if_met, str):
                    evaluated_if_met = aeval(evaluated_if_met)
                # if the evaluation failed, raise an exception
                if aeval.error_msg and if_conf == quoted_if_conf:
                    self.logger.error(
                        f"Failed to evaluate if condition, you probably used a variable that doesn't exist. Condition: {quoted_if_conf}, Rendered: {if_met}, Error: {aeval.error_msg}",
                        extra={
                            "condition": quoted_if_conf,
                            "rendered": if_met,
                            "step_id": self.step_id,
                        },
                    )
                    raise Exception(
                        f"Failed to evaluate if condition, you probably used a variable that doesn't exist. Condition: {if_conf}, Rendered: {if_met}, Error: {aeval.error_msg}"
                    )
                # maybe its because of quoting, try again without quoting
                elif aeval.error_msg or aeval.error:
                    # without quoting
                    aeval_without_quote = Interpreter()
                    if_met = self.io_handler.render(if_conf, safe=False)
                    evaluated_if_met = aeval_without_quote(if_met)
                    if isinstance(evaluated_if_met, str):
                        evaluated_if_met = aeval_without_quote(evaluated_if_met)
                    # if again error, raise an exception
                    if aeval_without_quote.error_msg:
                        raise Exception(
                            f"Failed to evaluate if condition, you probably used a variable that doesn't exist. Condition: {if_conf}, Rendered: {if_met}, Error: {aeval_without_quote.error_msg}"
                        )
            except Exception:
                missing_dep = self._check_missing_step_results()
                if missing_dep:
                    return self._mark_missing_step_result(missing_dep)
                raise
        else:
            evaluated_if_met = True

        action_name = self.config.get("name")
        if not evaluated_if_met:
            self.logger.info(
                f"Action {action_name} evaluated NOT to run, Reason: {if_met} evaluated to false. [before evaluation: {if_conf}]",
                extra={
                    "condition": if_conf,
                    "rendered": if_met,
                    "step_id": self.step_id,
                },
            )
            return None

        if if_conf:
            self.logger.info(
                f"Action {action_name} evaluated to run! Reason: {if_met} evaluated to true.",
                extra={
                    "condition": if_conf,
                    "rendered": if_met,
                    "step_id": self.step_id,
                },
            )
        else:
            self.logger.info(
                "Action %s evaluated to run! Reason: no condition, hence true.",
                self.config.get("name"),
                extra={
                    "step_id": self.step_id,
                },
            )

        # Now that we know the step is actually going to run, render and set its aliases.
        # This must happen after the "if" check above (and not before, and not
        # unconditionally) since aliases are only meaningful/safe to compute when the
        # step runs - e.g. they may reference results of another step that was skipped,
        # in which case rendering them here (before the "if" gate) could throw.
        aliases = dict(self.config.get("alias", {}))
        try:
            for alias_key, alias_val in aliases.items():
                aliases[alias_key] = self.io_handler.render(alias_val)
        except Exception:
            missing_dep = self._check_missing_step_results()
            if missing_dep:
                return self._mark_missing_step_result(missing_dep)
            raise

        self.context_manager.set_step_vars(
            self.step_id, _vars=self.vars, _aliases=aliases
        )

        # Third, check throttling
        # Now check if throttling is enabled
        self.logger.info(
            "Checking throttling for action %s",
            self.config.get("name"),
            extra={
                "step_id": self.step_id,
            },
        )
        throttled = self._check_throttling(self.config.get("name"))
        if throttled:
            self.logger.info(
                "Action %s is throttled",
                self.config.get("name"),
                extra={
                    "step_id": self.step_id,
                },
            )
            return False
        self.logger.info(
            "Action %s is not throttled",
            self.config.get("name"),
            extra={
                "step_id": self.step_id,
            },
        )

        # Last, run the action
        rendered_providers_parameters = None
        try:
            for curr_retry_count in range(self.__retry_count + 1):
                self.logger.info(
                    f"Running {self.step_id} {self.step_type}, current retry: {curr_retry_count}",
                    extra={
                        "step_id": self.step_id,
                    },
                )
                try:
                    from keep.api.core.incident_configuration import check_workflow_configuration
                    check_workflow_configuration(self.context_manager, self.db_session)
                    automation = getattr(self.context_manager, "automation_operation", None)
                    if automation and self.step_type == StepType.ACTION and self.notification is True and not automation["notification_allowed"]:
                        return self._mark_silenced([], automation["notification_skip_reason"])
                    # Silence gate check right before calling provider on each retry attempt
                    if self.step_type == StepType.ACTION and self.notification is True:
                        gate_result = self._notification_gate()
                        if gate_result is not None:
                            return gate_result

                    if not dont_render:
                        rendered_providers_parameters = self.io_handler.render_context(
                            self.provider_parameters
                        )
                    # special case for Keep provider (alert evaluation engine)
                    # which needs to evaluate the provider parameters by itself
                    else:
                        rendered_providers_parameters = self.provider_parameters

                    if automation:
                        from keep.api.core.incident_automation import check_automation_context
                        check_automation_context(self.context_manager, before_provider=True)
                        if self.step_type == StepType.ACTION and self.notification is True:
                            if not automation["notification_allowed"]:
                                return self._mark_silenced([], automation["notification_skip_reason"])
                            gate_result = self._notification_gate()
                            if gate_result is not None:
                                return gate_result

                    if self.step_type == StepType.STEP:
                        step_output = self.provider.query(
                            **rendered_providers_parameters
                        )
                    else:
                        step_output = self.provider.notify(
                            **rendered_providers_parameters
                        )
                    # exiting the loop as step/action execution was successful
                    self.context_manager.set_step_context(
                        self.step_id, results=step_output, foreach=self.foreach
                    )
                    step_context = self.context_manager.steps_context.get(self.step_id)
                    if isinstance(step_context, dict):
                        step_context["skipped"] = False
                        for key in ("skip_reason", "silence_reasons", "missing_from_step"):
                            step_context.pop(key, None)
                    break
                except Exception as e:
                    missing_dep = self._check_missing_step_results()
                    if missing_dep:
                        return self._mark_missing_step_result(missing_dep)
                    if curr_retry_count == self.__retry_count:
                        raise StepError(e)
                    else:
                        self.logger.info(
                            "Retrying running %s step after %s second(s)...",
                            self.step_id,
                            self.__retry_interval,
                            extra={
                                "step_id": self.step_id,
                            },
                        )

                        time.sleep(self.__retry_interval)

            extra_context = self.provider.expose()
            if rendered_providers_parameters is not None:
                rendered_providers_parameters.update(extra_context)
                self.context_manager.set_step_provider_paremeters(
                    self.step_id, rendered_providers_parameters
                )
        except Exception as e:
            missing_dep = self._check_missing_step_results()
            if missing_dep:
                return self._mark_missing_step_result(missing_dep)
            raise StepError(e)

        return True


class StepError(Exception):
    pass
