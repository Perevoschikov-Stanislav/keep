"""Deterministic notification policy inheritance over the published IaC bundle."""

import copy
import re
from itertools import product

from keep.api.core.incident_contract import SCHEMA, ContractError, require, validate_template, validate_link


SCOPES = ("team", "route", "family", "level")
SELECTORS = ("team_id", "route_ref", "family_ref", "level_id")
EVENTS = tuple(SCHEMA["$defs"]["NotificationEventType"]["enum"])
VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}")
PUBLIC_PATHS = {"keep_url", "count", "objects", "quiet_for", "status", "severity", "silenced"}
PUBLIC_PATHS |= {"incident." + key for key in (
    "id", "status", "severity", "team_id", "assignee", "alerts_count", "name", "summary", "episode", "revision",
    "start_time", "end_time", "created_at")}
PUBLIC_PATHS |= {"incident.automation." + key for key in (
    "level", "ack_breached", "ack_deadline_at", "origin", "episode")}
PUBLIC_PATHS |= {"incident.flapping.active", "incident.flapping.transition_count"}
PUBLIC_PATHS |= {"normalized." + key for key in (
    "cluster", "environment", "namespace", "kind", "resource", "workload", "service", "routing_level")}
PUBLIC_PATHS |= {"event." + key for key in (
    "type", "count", "objects", "quiet_for", "actor", "occurred_at", "previous_status", "previous_assignee")}
PUBLIC_PATHS |= {"target.title", "target.posted_at", "target.url"}


def configured(bundle):
    return bool(bundle.get("notification_defaults") or bundle.get("notification_policies"))


def _merge(target, patch, sources, origin, prefix=""):
    for key, value in patch.items():
        path = prefix + "." + key if prefix else key
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value, sources, origin, path)
        else:
            target[key] = copy.deepcopy(value)
            sources[path] = origin


def effective_policy(bundle, *, team_id, route_id, family_id=None, level_id=None):
    settings, sources = {}, {}
    _merge(settings, bundle.get("notification_defaults", {}), sources, "notification_defaults")
    selected = (team_id, route_id, family_id, level_id)
    seen = set()
    for scope in SCOPES:
        length = SCOPES.index(scope) + 1
        for policy in bundle.get("notification_policies", []):
            if policy["scope"] != scope:
                continue
            key = (scope, *(policy[name] for name in SELECTORS[:length]))
            require(key not in seen, "notification_policies", "ambiguous notification scope")
            seen.add(key)
            if key[1:] == selected[:length]:
                _merge(settings, policy["settings"], sources, policy["id"])
    return {"settings": settings, "sources": sources}


def condition(expression, context):
    if not expression or expression == "true":
        return True
    if expression == "false":
        return False
    import celpy
    environment = celpy.Environment()
    result = environment.program(environment.compile(expression)).evaluate(celpy.json_to_cel(context))
    if not isinstance(result, (bool, celpy.celtypes.BoolType)):
        raise ValueError("notification condition must return boolean")
    return bool(result)


def render_text(template, context):
    def replace(match):
        value = context
        for part in match.group(1).split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, list):
            return ", ".join(str(item) for item in value[:100])[:8192]
        return str(value)[:8192] if isinstance(value, (str, bool, int, float)) else "unknown"
    return VARIABLE.sub(replace, template)[:8192]


def _template(template, location, collection_paths=()):
    validate_template(template, location)
    require(set(VARIABLE.findall(template)) <= PUBLIC_PATHS | set(collection_paths),
            location, "unsupported or sensitive notification template field")


def policy_contexts(bundle):
    """Enumerate reachable inheritance paths, including each parent's default path."""
    result = []
    for route in bundle.get("routes", []):
        for team_id in route["team_ids"]:
            families = [None] + [item for item in bundle.get("correlation", []) if team_id in item["team_ids"]]
            for family in families:
                automation = next((item for item in bundle.get("automation", [])
                    if family and item["id"] == family.get("automation_ref")), None)
                levels = [None] + [item["id"] for item in automation["levels"]] if automation else [None]
                for level_id in levels:
                    result.append({"team_id": team_id, "route_id": route["id"],
                                   "family_id": family["id"] if family else None, "level_id": level_id})
                    require(len(result) <= 4096, "notification_policies", "too many notification inheritance paths")
    return result


def delivery_actions(rule, settings, capabilities):
    actions, fallbacks = [], []
    for action in rule.get("do", ["none"]):
        supported = (action in {"post", "none"} or
                     action == "edit" and capabilities["update"] or
                     action == "thread" and capabilities.get("thread", False) and capabilities["receipts"] or
                     action == "repost" and capabilities["update"] and capabilities["receipts"])
        if not supported:
            replacement = settings.get("fallbacks", {}).get(action)
            require(replacement is not None, "notification_policies." + action,
                    "transport requires an explicit fallback")
            fallbacks.append({"from": action, "to": replacement})
            action = replacement
        if action != "none" and action not in actions:
            actions.append(action)
    return actions, fallbacks


def resolve_for_incident(bundle, route, view):
    correlation = view.get("correlation") or {}
    automation = view.get("automation") or {}
    return effective_policy(bundle, team_id=view["team_id"], route_id=route["id"],
                            family_id=correlation.get("rule_id"), level_id=automation.get("level"))


def event_context(view, event_type, extra=None, *, silenced=False):
    event = {"type": event_type, "count": 1, "objects": [view.get("name", "Incident")],
             "quiet_for": "unknown", "actor": "system", **(extra or {})}
    return {**view, "incident": view, "event": event, "count": event["count"],
            "objects": event["objects"], "quiet_for": event["quiet_for"], "silenced": silenced}


def event_rule(settings, route, event_type):
    legacy = "edit" if route.get("delivery_mode") == "upsert" else "post"
    return {"do": [legacy], "when": "true", "enabled": True,
            **settings.get("events", {}).get(event_type, {})}


def event_decision(rule, context):
    if not rule.get("enabled", True):
        return "event_disabled"
    try:
        if not condition(rule.get("when"), context):
            return "event_condition_false"
        if condition(rule.get("stop_when", "false"), context):
            return "event_stop_condition"
    except Exception:
        return "event_condition_error"
    return None


def selected_actions(rule, settings, capabilities, context):
    rule = copy.deepcopy(rule)
    if "within_seconds" in rule and context["event"].get("elapsed_since_resolution_seconds", 0) > rule["within_seconds"]:
        rule["do"] = rule.get("else_do", ["none"])
    return delivery_actions(rule, settings, capabilities)


def presentation_definition(bundle, route, settings):
    definition = copy.deepcopy(next(item for item in bundle["presentations"]
                                  if item["id"] == settings.get("presentation_ref", route["presentation_ref"])))
    _merge(definition, settings.get("card", {}), {}, "card")
    if "buttons" in settings:
        definition["actions"] = copy.deepcopy(settings["buttons"])
    return definition


def validate_policies(bundle, teams, *, compile_expressions=False):
    if not configured(bundle):
        return
    policies = bundle.get("notification_policies", [])
    routes = {item["id"]: item for item in bundle.get("routes", [])}
    families = {item["id"]: item for item in bundle.get("correlation", [])}
    presentations = {item["id"]: item for item in bundle.get("presentations", [])}
    contacts = {item["id"]: item for item in bundle.get("contacts", [])}
    seen = set()
    for policy in policies:
        path = "notification_policies." + policy["id"]
        length = SCOPES.index(policy["scope"]) + 1
        key = (policy["scope"], *(policy[name] for name in SELECTORS[:length]))
        require(key not in seen, path, "ambiguous notification scope")
        seen.add(key)
        require(policy["team_id"] in teams, path, "unknown team")
        if length >= 2:
            route = routes.get(policy["route_ref"])
            require(route and policy["team_id"] in route["team_ids"], path, "route does not cover policy team")
        if length >= 3:
            family = families.get(policy["family_ref"])
            require(family and policy["team_id"] in family["team_ids"], path, "family does not cover policy team")
        if length == 4:
            automation = next((item for item in bundle.get("automation", []) if item["id"] == family.get("automation_ref")), None)
            require(automation and policy["level_id"] in {item["id"] for item in automation["levels"]},
                    path, "unknown family escalation level")
    for settings, location in [(bundle.get("notification_defaults", {}), "notification_defaults")] + [
            (item["settings"], "notification_policies." + item["id"] + ".settings") for item in policies]:
        require(not settings.get("presentation_ref") or settings["presentation_ref"] in presentations,
                location + ".presentation_ref", "unknown presentation")
        for event, rule in settings.get("events", {}).items():
            require(set(rule.get("contact_refs", [])) <= set(contacts), location, "unknown event contact")
        collection_paths = {"incident.collections." + collection["id"] for item in presentations.values()
                            for collection in item.get("collections", [])}
        templates = [(value, location + ".lines." + event) for event, value in settings.get("lines", {}).items()]
        if "stub" in settings and "text" in settings["stub"]:
            templates.append((settings["stub"]["text"], location + ".stub.text"))
        card = settings.get("card", {})
        templates += [(card[key], location + ".card." + key) for key in ("title", "description", "footer") if key in card]
        templates += [(value, location + ".card.tags") for value in card.get("tags", [])]
        for value, path in templates:
            _template(value, path, collection_paths)
        for field in card.get("fields", []):
            require(field["path"] in PUBLIC_PATHS | collection_paths, location + ".card.fields", "unsupported field")
        for link in card.get("links", []):
            _template(link["url_template"], location + ".card.links", collection_paths)
            validate_link(link["url_template"], location + ".card.links")
        expressions = [rule[key] for rule in settings.get("events", {}).values() for key in ("when", "stop_when") if key in rule]
        expressions += [item["when"] for item in settings.get("buttons", []) + card.get("fields", []) if "when" in item]
        if compile_expressions:
            import celpy
            for expression in expressions:
                try:
                    celpy.Environment().compile(expression)
                except Exception:
                    raise ContractError(location + ": invalid notification CEL") from None
    destinations = {item["id"]: item for item in bundle.get("destinations", [])}
    transports = {item["id"]: item for item in bundle.get("transports", [])}
    for selectors in policy_contexts(bundle):
        settings = effective_policy(bundle, **selectors)["settings"]
        route = routes[selectors["route_id"]]
        for ref in route["destination_refs"]:
            destination = destinations[ref]
            if destination["team_id"] != selectors["team_id"]:
                continue
            capabilities = transports[destination["transport_ref"]]["capabilities"]
            for event, rule in settings.get("events", {}).items():
                delivery_actions(rule, settings, capabilities)
                if "else_do" in rule:
                    delivery_actions({"do": rule["else_do"]}, settings, capabilities)
            for button in settings.get("buttons", []):
                require(button.get("mode") != "one_click" or capabilities["actions"] or
                        button.get("identity_fallback", "confirm_in_keep") == "confirm_in_keep",
                        "notification_policies.buttons", "one_click requires a verified identity adapter or confirmation fallback")


def preview_notifications(bundle):
    """Deterministic synthetic examples; applying a bundle does not send them."""
    if not configured(bundle):
        return []
    destinations = {item["id"]: item for item in bundle.get("destinations", [])}
    transports = {item["id"]: item for item in bundle.get("transports", [])}
    presentations = {item["id"]: item for item in bundle.get("presentations", [])}
    routes = {item["id"]: item for item in bundle.get("routes", [])}
    result = []
    for selectors in policy_contexts(bundle):
        resolved = effective_policy(bundle, **selectors)
        settings = resolved["settings"]
        route = routes[selectors["route_id"]]
        definition = copy.deepcopy(presentations[settings.get("presentation_ref", route["presentation_ref"])])
        _merge(definition, settings.get("card", {}), {}, "card")
        for event, ref in product(settings.get("events", {}), route["destination_refs"]):
            destination = destinations[ref]
            if destination["team_id"] != selectors["team_id"]:
                continue
            capabilities = transports[destination["transport_ref"]]["capabilities"]
            actions, fallbacks = delivery_actions(settings["events"][event], settings, capabilities)
            context = {"incident": {"id": "00000000-0000-0000-0000-000000000001", "name": "Example incident",
                "status": "firing", "severity": "high", "team_id": selectors["team_id"], "assignee": "example-user",
                "alerts_count": 2, "episode": 1, "revision": 1, "collections": {},
                "automation": {"level": selectors["level_id"]}, "flapping": {"active": False, "transition_count": 0}},
                "event": {"type": event, "count": 2, "objects": ["resource-a", "resource-b"], "quiet_for": "3h",
                          "actor": "example-user", "occurred_at": "2000-01-01T00:00:00Z"},
                "normalized": {"kind": "storage", "resource": "resource-a", "namespace": "example",
                               "cluster": "example-cluster", "workload": "example-workload", "service": "example-service"},
                "target": {"title": "Example incident", "posted_at": "2000-01-01T00:00:00Z", "url": bundle.get("keep_url", "https://keep.example.org")},
                "keep_url": bundle.get("keep_url", "https://keep.example.org"),
                "count": 2, "objects": ["resource-a", "resource-b"], "quiet_for": "3h", "status": "firing", "severity": "high", "silenced": False}
            result.append({**selectors, "event_type": event, "destination_ref": ref, "settings": settings,
                "sources": resolved["sources"], "actions": actions, "fallbacks": fallbacks,
                "card": {key: render_text(definition.get(key, ""), context) for key in ("title", "description", "footer")},
                "line": render_text(settings.get("lines", {}).get(event, ""), context),
                "stub": render_text(settings.get("stub", {}).get("text", ""), context)})
            require(len(result) <= 4096, "notification_preview", "too many rendered notification examples")
    return result
