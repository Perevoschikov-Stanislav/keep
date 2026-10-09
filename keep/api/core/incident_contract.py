"""Shared incident IaC contract validation. This module never provisions resources."""

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = ROOT / "docs/fork/incident-core"
SCHEMA = json.loads(Path(__file__).with_name("incident_contract_v1.schema.json").read_text())
FORMAT_CHECKER = FormatChecker()
PROTECTED = {"tenant_id", "team_id", "role", "groups", "actor", "created_by", "updated_by", "fingerprint", "correlation", "correlation_context", "lifecycle", "lifecycle_context", "automation", "automation_context", "notification_context"}
SENSITIVE = {"secret", "secrets", "providers", "authentication", "authorization", "password", "token", "api_key", "cookie"}


class ContractError(ValueError):
    """Safe error: paths/codes only, never incoming values or credentials."""


class UniqueKeyLoader(yaml.SafeLoader):
    pass


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def valid_datetime(value):
    return not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ContractError(f"YAML line {key_node.start_mark.line + 1}: duplicate/non-string key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_yaml(content):
    try:
        value = yaml.load(content, Loader=UniqueKeyLoader)
        # Reject cycles, timestamps/non-JSON types and non-finite values before walking schemas.
        json.dumps(value, allow_nan=False)
        return value
    except ContractError:
        raise
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise ContractError("artifact: unreadable or invalid JSON-compatible YAML") from None


def read_yaml(path):
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ContractError("artifact: unreadable YAML") from None
    return parse_yaml(content)


def validator(name):
    return Draft202012Validator(
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{name}"},
        format_checker=FORMAT_CHECKER,
    )


def validate_shape(name, value, location):
    errors = sorted(validator(name).iter_errors(value), key=lambda e: str(list(e.absolute_path)))
    if errors:
        error = errors[0]
        path = location
        current = value
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else "." + str(part)
            current = current[part]
            if isinstance(current, dict) and isinstance(current.get("id"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", current["id"]):
                path += f"(id={current['id']})"
        code = f"{error.validator} constraint failed"
        if error.validator == "additionalProperties" and isinstance(current, dict):
            extra = set(current) - set(error.schema.get("properties", {}))
            names = sorted(key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) else "<invalid field>" for key in extra)
            code = "unknown field(s): " + ", ".join(names)
        elif error.validator == "required" and isinstance(current, dict):
            code = "missing field(s): " + ", ".join(sorted(set(error.validator_value) - set(current)))
        raise ContractError(f"{path}: {code}")
    if name in {"Notification", "ReadyAction"}:
        validate_link_url(value["keep_url"], location + ".keep_url")
    if name == "ReadyLink":
        validate_link_url(value["url"], location + ".url")
    if name == "Notification":
        for link in value["links"]:
            validate_link_url(link["url"], location + ".links.url")
        for action in value["actions"]:
            validate_link_url(action["keep_url"], location + ".actions.keep_url")


def require(condition, location, code):
    if not condition:
        raise ContractError(f"{location}: {code}")


def sensitive_key(key):
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE or normalized.endswith(("_secret", "_token", "_password", "_api_key", "_private_key"))


def reject_sensitive(value, location):
    if isinstance(value, dict):
        for key, item in value.items():
            require(not sensitive_key(key), location, "inline credential field forbidden; use a reference")
            reject_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_sensitive(item, f"{location}[{index}]")


def validate_endpoint(value, location):
    try:
        endpoint = urlsplit(value)
        require(endpoint.scheme in {"http", "https"} and endpoint.hostname is not None and
                not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment,
                location, "invalid endpoint")
        require(endpoint.port is None or 1 <= endpoint.port <= 65535, location, "invalid port")
        require(".." not in endpoint.path.split("/"), location, "path traversal forbidden")
    except ValueError:
        raise ContractError(f"{location}: invalid endpoint") from None


def validate_link_url(value, location):
    try:
        link = urlsplit(value)
        require(link.scheme in {"http", "https"} and link.hostname and not link.username and not link.password,
                location, "link credentials/unsupported scheme forbidden")
        require(link.port is None or 1 <= link.port <= 65535, location, "invalid link port")
        for key, _ in parse_qsl(link.query) + parse_qsl(link.fragment):
            require(not sensitive_key(unquote(key)), location, "credential query/fragment forbidden")
    except ValueError:
        raise ContractError(f"{location}: invalid link") from None


def validate_template(value, location):
    expressions = re.findall(r"{{\s*(.*?)\s*}}", value)
    for expression in expressions:
        # Contract deliberately uses the existing simple mustache field syntax.
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", expression) is not None,
                location, "unsupported template expression")
        require(not any(sensitive_key(part) for part in expression.split(".")), location, "credential expansion forbidden")
    remaining = re.sub(r"{{.*?}}", "", value)
    require("{{" not in remaining and "}}" not in remaining, location, "unbalanced template")


def validate_link(value, location):
    validate_template(value, location)
    require(value.startswith(("http://", "https://", "{{ keep_url }}")), location, "unsupported link scheme")
    # Replace only placeholders; public dashboard query parameters are valid links.
    rendered = re.sub(r"{{\s*keep_url\s*}}", "https://keep.example.org", value)
    rendered = re.sub(r"{{.*?}}", "value", rendered)
    validate_link_url(rendered, location)


def artifact(directory, document, location, contents=None, verified_contents=None):
    validate_shape("Artifact", document, location)
    require(".." not in document["path"].split("/"), location + ".path", "path traversal forbidden")
    path = (directory / document["path"]).resolve()
    require(path.is_relative_to(directory.resolve()), location + ".path", "artifact escapes bundle directory")
    try:
        content = path.read_bytes() if contents is None else contents[document["path"]].encode("utf-8")
    except (OSError, KeyError, UnicodeError):
        raise ContractError(f"{location}.path: artifact unavailable; retain active configuration") from None
    require(hashlib.sha256(content).hexdigest() == document["sha256"], location + ".sha256", "artifact digest mismatch")
    if verified_contents is not None:
        try:
            verified_contents[document["path"]] = content.decode("utf-8")
        except UnicodeError:
            raise ContractError(location + ": UTF-8 artifact required") from None
    # Parse exactly the bytes whose digest was checked, even if a mounted file changes now.
    return parse_yaml(content)


def indexed(items, location):
    result = {}
    for index, item in enumerate(items):
        require(item["id"] not in result, f"{location}[{index}].id", "duplicate logical ID")
        result[item["id"]] = item
    return result


def validate_policy(document):
    validate_shape("TeamPolicy", document, "access")
    require(type(document["version"]) is int, "access.version", "integer version required")
    teams = indexed(document.get("teams", []), "access.teams")
    zones = set()
    for team in teams.values():
        path = "access.teams[" + json.dumps(team["id"], ensure_ascii=True) + "]"
        require(not zones.intersection(team.get("zones", [])), path + ".zones", "zone belongs to multiple teams")
        zones.update(team.get("zones", []))
        require(set(team.get("visible_to", [team["id"]])) <= set(teams), path + ".visible_to", "unknown team reference")
    views = indexed(document.get("incident_views", []), "access.incident_views")
    require("all" not in views, "access.incident_views.id", "all is reserved")
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    from keep.identitymanager.team_policy import TeamPolicy

    try:
        policy = TeamPolicy(document)
    except (KeyError, TypeError, ValueError):
        raise ContractError("access: existing TeamPolicy rejected this document") from None
    groups = set()
    for role, members in document.get("roles", {}).items():
        require(not groups.intersection(members), f"access.roles.{role}", "same role group assigned to multiple roles")
        groups.update(members)
    return policy


def validate_workflow_shape(document, location):
    # Existing workflow/provider syntax is retained. Runtime providers validate their own parameters in 24.
    require(isinstance(document, dict) and set(document) == {"workflow"}, location, "single existing workflow wrapper required")
    workflow = document["workflow"]
    allowed = {"id", "name", "description", "disabled", "debug", "triggers", "inputs", "consts", "strategy", "tags", "interval",
               "on-failure", "owners", "permissions", "services", "steps", "actions"}
    require(isinstance(workflow, dict) and not set(workflow).difference(allowed), location, "unknown workflow field")
    require(isinstance(workflow.get("id"), str) and workflow["id"], location + ".workflow.id", "workflow ID required")
    require(workflow.get("strategy", "nonparallel_with_retry") in ("parallel", "nonparallel", "nonparallel_with_retry"),
            location + ".workflow.strategy", "unsupported workflow strategy")
    require(isinstance(workflow.get("triggers"), list) and workflow["triggers"], location + ".workflow.triggers", "triggers required")
    steps = []
    for section in ("steps", "actions"):
        items = workflow.get(section, [])
        require(isinstance(items, list), location + f".workflow.{section}", "list required")
        steps.extend(items)
    require(steps, location, "workflow requires a step/action")
    for index, step in enumerate(steps):
        path = f"{location}.workflow.steps/actions[{index}]"
        require(isinstance(step, dict) and isinstance(step.get("name"), str), path, "named step required")
        require("notification" not in step or type(step["notification"]) is bool, path + ".notification", "boolean required")
        provider = step.get("provider")
        require(isinstance(provider, dict) and isinstance(provider.get("type"), str) and
                isinstance(provider.get("with"), dict), path + ".provider", "existing provider block required")
        # Literal secret fields are forbidden; existing {{ secrets.* }} references are permitted here only.
        def secret_fields(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if sensitive_key(key):
                        require(isinstance(item, str) and re.fullmatch(r"(?:Bearer |Basic )?{{\s*secrets\.[A-Za-z0-9_.-]+\s*}}", item),
                                path, "workflow credential must be a secret reference")
                    else:
                        secret_fields(item)
            elif isinstance(value, list):
                for item in value:
                    secret_fields(item)
        secret_fields(provider)


def validate_bundle(bundle, directory, expected_tenant, compatibility=False, *, include_documents=False, artifact_contents=None):
    validate_shape("Bundle", bundle, "bundle")
    require(bundle["tenant_id"] == expected_tenant, "bundle.tenant_id", "does not match trusted apply tenant")
    reject_sensitive(bundle, "bundle")
    validate_endpoint(bundle["keep_url"], "bundle.keep_url")
    verified_contents = {}
    documents = {"access": artifact(directory, bundle["access"], "bundle.access", artifact_contents, verified_contents)}
    policy = validate_policy(documents["access"])
    teams = set(policy.teams) | {None}
    ownership_teams = set()
    for entry in bundle.get("runtime_ownership", []):
        require(entry["team_id"] in teams and entry["team_id"] not in ownership_teams,
                "runtime_ownership.team_id", "unknown or duplicate team")
        ownership_teams.add(entry["team_id"])
        require(entry["notifications"] != "keep" or entry["domain"] == "keep",
                "runtime_ownership", "Keep notifications require Keep domain ownership")
        require(entry["domain"] != "keep" or entry["legacy_snooze"] == "disabled",
                "runtime_ownership", "Keep domain requires retired legacy snooze")
    sections = ("mappings", "workflows", "extraction", "rules", "normalization", "presentations", "correlation", "lifecycle", "automation",
                "contacts", "routes", "notification_policies", "transports", "destinations", "proof_profiles", "service_clients", "subscribers")
    resources = {name: indexed(bundle.get(name, []), name) for name in sections}
    removals = set()
    for removal in bundle.get("deletions", []):
        key = (removal["kind"], removal["id"])
        require(key not in removals, "deletions", "duplicate removal")
        removals.add(key)
        existing = policy.teams if removal["kind"] == "teams" else resources[removal["kind"]]
        require(removal["id"] not in existing, "deletions", "resource cannot be present and removed in one bundle")
    seen_paths = {bundle["access"]["path"]}
    for section in ("mappings", "workflows", "extraction", "rules"):
        legacy_names = set()
        for item in resources[section].values():
            path = f"{section}.{item['id']}"
            filename = item["artifact"]["path"]
            require(filename not in seen_paths, path + ".artifact.path", "artifact reused under another logical resource")
            seen_paths.add(filename)
            content = artifact(directory, item["artifact"], path + ".artifact", artifact_contents, verified_contents)
            documents[f"{section}.{item['id']}"] = content
            if section == "mappings":
                validate_shape("MappingManifest", content, path)
                require(content["name"] not in legacy_names, path + ".name", "ambiguous existing mapping name")
                legacy_names.add(content["name"])
                for row in content.get("rows") or []:
                    require(not ({key.split('.')[0] for key in row} & PROTECTED), path + ".rows", "mapping cannot write canonical security fields")
                    reject_sensitive(row, path + ".rows")
                    require("zone" not in row or row["zone"] in policy.zone_to_team, path + ".rows.zone", "unknown ownership zone")
                require(content.get("type", "csv") != "csv" or content.get("rows"), path + ".rows", "CSV rows required")
                if content.get("is_multi_level", False):
                    require(content.get("new_property_name") and len(content["matchers"]) == 1,
                            path, "multi-level mapping requires one matcher group and new_property_name")
                    output = content["new_property_name"].split(".")
                    require(output[0] not in PROTECTED and not any(sensitive_key(part) for part in output), path, "unsafe mapping output")
            elif section == "workflows":
                validate_workflow_shape(content, path)
                require(content["workflow"]["id"] == item["id"], path + ".id", "existing workflow ID must equal logical ID")
                name = content["workflow"].get("name", item["id"])
                require(isinstance(name, str) and name not in legacy_names, path + ".name", "invalid/ambiguous existing workflow name")
                legacy_names.add(name)
            else:
                require(isinstance(content, dict), path, "existing rule artifact must be an object")
    if artifact_contents is not None:
        require(set(artifact_contents) == seen_paths, "artifacts", "missing or unreferenced artifact")

    def reference(name, logical_id, location):
        require(logical_id in resources[name], location, f"unknown {name} reference")
        return resources[name][logical_id]

    def scope(item, location):
        selected = set(item.get("team_ids", [item.get("team_id")]))
        require(selected <= teams, location + ".team_ids/team_id", "unknown team")
        return selected

    def destinations_and_contacts(item, location, selected):
        for name, field in (("destinations", "destination_refs"), ("contacts", "contact_refs")):
            for logical_id in item.get(field, []):
                target = reference(name, logical_id, location + "." + field)
                require(target["team_id"] in selected, location + "." + field, "reference crosses team scope")

    for name in sections:
        for item in resources[name].values():
            if "team_ids" in item or "team_id" in item:
                scope(item, f"{name}.{item['id']}")

    for transport in resources["transports"].values():
        path = f"transports.{transport['id']}"
        validate_endpoint(transport["endpoint"], path + ".endpoint")
        auth = transport["auth_ref"]
        if auth and auth.startswith("file:"):
            require(".." not in auth.split("/"), path + ".auth_ref", "secret reference traversal forbidden")
        retry = transport.get("delivery", {}).get("retry", {})
        require(retry.get("initial_backoff_seconds", 2) <= retry.get("max_backoff_seconds", 60),
                path + ".delivery.retry", "initial backoff exceeds cap")
        require(bundle.get("dispatch", {}).get("lease_seconds", 60) > transport.get("delivery", {}).get("timeout_seconds", 10),
                path + ".delivery.timeout_seconds", "send timeout must be shorter than dispatcher lease")
        client = transport.get("callback_client_ref")
        if client is not None:
            reference("service_clients", client, path + ".callback_client_ref")
        require(not transport["capabilities"]["actions"] or client is not None,
                path + ".callback_client_ref", "interactive actions require registered service identity/proof")

    for destination in resources["destinations"].values():
        path = f"destinations.{destination['id']}"
        transport = reference("transports", destination["transport_ref"], path + ".transport_ref")
        validate_shape("MattermostDestinationOptions" if transport["kind"] == "mattermost" else "HttpDestinationOptions",
                       destination["options"], path + ".options")
        if "path" in destination["options"]:
            require(".." not in destination["options"]["path"].split("/"), path + ".options.path", "path traversal forbidden")
        if transport["capabilities"]["actions"]:
            client = resources["service_clients"][transport["callback_client_ref"]]
            require(destination["team_id"] in client["team_ids"], path, "callback client does not cover destination team")

    for profile in resources["proof_profiles"].values():
        path = f"proof_profiles.{profile['id']}"
        validate_endpoint(profile["issuer"], path + ".issuer")
        validate_endpoint(profile["jwks_url"], path + ".jwks_url")
        require(profile.get("groups_claim", "groups") not in PROTECTED - {"groups"}, path + ".groups_claim", "invalid group claim")
        require(not set(profile["required_claims"]) & {"iss", "aud", "sub", "iat", "exp", "nbf", "groups", "azp", "client_id"},
                path + ".required_claims", "standard identity/time claims are verified separately")
    origins, auth_refs = set(), set()
    for client in resources["service_clients"].values():
        path = f"service_clients.{client['id']}"
        require(client["origin"] not in origins and client["auth_ref"] not in auth_refs, path, "ambiguous service identity/origin")
        origins.add(client["origin"])
        auth_refs.add(client["auth_ref"])
        require(".." not in client["auth_ref"].split("/"), path + ".auth_ref", "secret reference traversal forbidden")
        require(not set(client["scopes"]) & {"write:silence", "update:silence", "update:incident"} or client["proof_profile_refs"],
                path + ".proof_profile_refs", "delegated mutation scopes require actor-proof profiles")
        for logical_id in client["proof_profile_refs"]:
            reference("proof_profiles", logical_id, path + ".proof_profile_refs")

    for contact in resources["contacts"].values():
        path = f"contacts.{contact['id']}"
        seen = set()
        for address in contact["addresses"]:
            reference("transports", address["transport_ref"], path + ".addresses.transport_ref")
            require(address["transport_ref"] not in seen, path + ".addresses", "duplicate transport address")
            seen.add(address["transport_ref"])

    for presentation in resources["presentations"].values():
        path = f"presentations.{presentation['id']}"
        for field in ("title", "description"):
            validate_template(presentation[field], path + "." + field)
        commands = [action["command"] for action in presentation.get("actions", [])]
        require(len(commands) == len(set(commands)), path + ".actions", "duplicate command presentation")
        for field in presentation.get("fields", []):
            require(not any(sensitive_key(part) for part in field["path"].split(".")), path + ".fields.path", "sensitive display field forbidden")
        for link in presentation.get("links", []):
            validate_link(link["url_template"], path + ".links.url_template")
        collections = presentation.get("collections", [])
        require(len({item["id"] for item in collections}) == len(collections), path + ".collections", "duplicate collection")
        for item in collections + presentation.get("source_links", []):
            for source in item["sources"]:
                require(not any(sensitive_key(part) for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)),
                        path + ".sources", "credential source forbidden")
                require(source.split(".")[0].split("[")[0] not in PROTECTED,
                        path + ".sources", "canonical security source forbidden")

    for normalization in resources["normalization"].values():
        path = f"normalization.{normalization['id']}"
        if normalization.get("presentation_ref"):
            reference("presentations", normalization["presentation_ref"], path + ".presentation_ref")
        seen = set()
        for field in normalization["fields"]:
            require(field["name"] not in seen, path + ".fields.name", "duplicate normalized field")
            seen.add(field["name"])
            for source in field["sources"]:
                require(not any(sensitive_key(part) for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)), path + ".fields.sources", "credential source forbidden")
            if "extract" in field:
                try:
                    regex = re.compile(field["extract"]["pattern"])
                except re.error:
                    raise ContractError(f"{path}.fields.extract.pattern: invalid regex") from None
                require(field["extract"].get("group", 1) <= regex.groups, path + ".fields.extract.group", "capture group unavailable")

    for lifecycle in resources["lifecycle"].values():
        require(lifecycle["reopen"]["mode"] != "new_incident" or lifecycle["reopen"]["ack"] == "reset",
                f"lifecycle.{lifecycle['id']}.reopen.ack", "new incident cannot inherit old ACK")

    for automation in resources["automation"].values():
        path = f"automation.{automation['id']}"
        selected = scope(automation, path)
        indexed(automation["levels"], path + ".levels")
        offsets = [level["after_seconds"] for level in automation["levels"]]
        require(offsets == sorted(set(offsets)), path + ".levels.after_seconds", "levels must have strictly increasing offsets")
        for level in automation["levels"]:
            destinations_and_contacts(level, path + ".levels", selected)
            for logical_id in level.get("workflow_refs", []):
                reference("workflows", logical_id, path + ".levels.workflow_refs")
            require(level.get("repeat_every_seconds", 0) != 0 or level.get("repeat_limit", 1) == 1,
                    path + ".levels.repeat_limit", "repeat limit requires positive repeat interval")
            require(level.get("destination_refs") or level.get("workflow_refs"), path + ".levels", "level has no executable action")
            require(level.get("workflow_refs") or all(any(resources["destinations"][ref]["team_id"] == team
                    for ref in level.get("destination_refs", [])) for team in selected),
                    path + ".levels.destination_refs", "level has no action for a selected team")
        if "reminder" in automation:
            destinations_and_contacts(automation["reminder"], path + ".reminder", selected)
        if "ticket" in automation:
            reference("workflows", automation["ticket"]["workflow_ref"], path + ".ticket.workflow_ref")
            validate_link(automation["ticket"]["url_template"], path + ".ticket.url_template")
            require(automation["ticket"].get("enrichment_key", "ticket_url") not in PROTECTED | {
                "id", "status", "assignee", "zone", "normalized", "normalization", "presentation", "generated_name"},
                path + ".ticket.enrichment_key", "reserved incident field")

    for rule in resources["correlation"].values():
        path = f"correlation.{rule['id']}"
        require(set(rule["group_by"]) <= set(rule["required_fields"]), path + ".required_fields", "must include every group_by field")
        lifecycle = reference("lifecycle", rule["lifecycle_ref"], path + ".lifecycle_ref")
        reference("presentations", rule["presentation_ref"], path + ".presentation_ref")
        for field in rule["group_by"] + rule["required_fields"]:
            parts = field.split(".")
            require(not set(parts) & PROTECTED and not any(sensitive_key(part) for part in parts),
                    path + ".group_by/required_fields", "security scope is added by core, not configurable key")
        if rule.get("multi_level_property_name"):
            parts = rule["multi_level_property_name"].split(".")
            require(not set(parts) & PROTECTED and not any(sensitive_key(part) for part in parts),
                    path + ".multi_level_property_name", "unsafe nested grouping field")
        if rule.get("automation_ref") is not None:
            automation = reference("automation", rule["automation_ref"], path + ".automation_ref")
            require(set(rule["team_ids"]) <= set(automation["team_ids"]), path + ".automation_ref", "automation does not cover rule teams")
            require(lifecycle["reopen"]["mode"] != "new_incident" or automation["on_reopen"] == "reset",
                    path + ".automation_ref", "new incident must reset SLA")

    for route in resources["routes"].values():
        path = f"routes.{route['id']}"
        require(route.get("after_silence", "none") != "current_active" or "incident.updated" in route["event_types"],
                path + ".after_silence", "current_active requires incident.updated in event_types")
        destinations_and_contacts(route, path, scope(route, path))
        presentation = reference("presentations", route["presentation_ref"], path + ".presentation_ref")
        for logical_id in route["destination_refs"]:
            destination = resources["destinations"][logical_id]
            transport = resources["transports"][destination["transport_ref"]]
            capabilities = transport["capabilities"]
            require(route["delivery_mode"] != "upsert" or capabilities["update"] or route["update_fallback"] == "append",
                    path + ".update_fallback", "adapter cannot update; explicit append fallback required")
            require(not presentation.get("actions") or capabilities["actions"] or route["actions_fallback"] == "keep_link",
                    path + ".actions_fallback", "adapter cannot submit actions; explicit Keep-link fallback required")
            for contact_id in route.get("contact_refs", []):
                contact = resources["contacts"][contact_id]
                if contact["team_id"] == destination["team_id"]:
                    require(destination["transport_ref"] in {address["transport_ref"] for address in contact["addresses"]},
                            path + ".contact_refs", "contact has no address for destination adapter")
    for subscriber in resources["subscribers"].values():
        destinations_and_contacts(subscriber, f"subscribers.{subscriber['id']}", scope(subscriber, "subscribers"))

    for section in ("normalization", "correlation", "routes"):
        priorities = defaultdict(set)
        for item in resources[section].values():
            for team in item["team_ids"]:
                for event in item.get("event_types", [None]):
                    require(item["priority"] not in priorities[(team, event)], f"{section}.{item['id']}.priority", "ambiguous priority within team/event scope")
                    priorities[(team, event)].add(item["priority"])

    from keep.api.core.notification_policies import validate_policies
    validate_policies(bundle, teams, compile_expressions=compatibility)

    if compatibility:
        validate_existing_runtime(resources, documents, bundle)
    canonical = {"bundle": bundle, "artifacts": documents}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    result = {"api_version": bundle["api_version"], "id": bundle["id"], "tenant_id": bundle["tenant_id"],
            "revision": bundle["revision"], "digest": digest,
            "resources": {name: len(items) for name, items in resources.items()}}
    if include_documents:
        result["documents"] = documents
        result["artifact_contents"] = verified_contents
    return result


def validate_existing_runtime(resources, documents, bundle):
    """Compile CEL and use the actual current Keep DTO/parsers; no DB or provider execution."""
    import celpy
    from keep.api.models.db.mapping import MappingRuleDtoIn
    from keep.workflowmanager.workflowstore import WorkflowStore

    for section in ("normalization", "correlation", "routes", "automation"):
        for item in resources[section].values():
            try:
                celpy.Environment().compile(item.get("match", "true"))
            except Exception:
                raise ContractError(f"{section}.{item['id']}.match: existing CEL compiler rejected expression") from None
    for view in documents["access"].get("incident_views", []):
        if view["cel"]:
            try:
                celpy.Environment().compile(view["cel"])
            except Exception:
                raise ContractError("access.incident_views.cel: existing CEL compiler rejected expression") from None
    for item in resources["mappings"].values():
        try:
            MappingRuleDtoIn(**documents[f"mappings.{item['id']}"])
        except Exception:
            raise ContractError(f"mappings.{item['id']}: current Keep DTO rejected artifact") from None
    for item in resources["workflows"].values():
        try:
            WorkflowStore.pre_parse_workflow_yaml(documents[f"workflows.{item['id']}"])
        except Exception:
            raise ContractError(f"workflows.{item['id']}: current Keep parser rejected artifact") from None


def parameter_inventory():
    rows = ["# Incident policies v1 parameters", "", "Generated by `python3 lab/incident_contract.py inventory` from schema.", "",
            "Defaults are not injected by validator. `required` — explicitly specified; `absent` — feature disabled/not applicable.",
            "All changes follow validate -> preview -> apply. Wire fields represent state/command, not configuration.", "",
            "| Field | Type / bounds | Default | Source | Effect / description |",
            "| --- | --- | --- | --- | --- |"]

    def display(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def walk(name, node, effect, source):
        for field, definition in node.get("properties", {}).items():
            target = SCHEMA["$defs"].get(definition.get("$ref", "").split("/")[-1], {})
            kind = definition.get("$ref", definition.get("type", "oneOf")).replace("#/$defs/", "") if isinstance(definition.get("$ref", definition.get("type", "oneOf")), str) else definition["type"]
            constraints = {key: definition[key] for key in ("enum", "const", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern") if key in definition}
            if not constraints:
                constraints = {key: target[key] for key in ("enum", "const", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern") if key in target}
            default = definition.get("default", "required" if field in node.get("required", []) else "absent")
            description = definition.get("description", target.get("description", "Logical resource reference."))
            rows.append("| " + " | ".join(map(display, [f"{name}.{field}", f"{kind} {json.dumps(constraints, ensure_ascii=False)}",
                                                        json.dumps(default, ensure_ascii=False), source, f"{effect}; {description}"])) + " |")
            walk(f"{name}.{field}", definition, effect, source)
    for name, definition in SCHEMA["$defs"].items():
        if "properties" not in definition:
            continue
        source = "Keep/adapter wire" if name in {"ReadyField", "ReadyAction", "ReadyLink", "Notification", "IncidentCommand", "DeliveryReceipt"} else "bundle/artifact YAML"
        walk(name, definition, definition["x-change"], source)
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validation = sub.add_parser("validate", help="Offline structural/reference validation; does not apply")
    validation.add_argument("bundle", type=Path)
    validation.add_argument("--tenant", required=True, help="Trusted expected tenant, independent of the YAML body")
    validation.add_argument("--compatibility", action="store_true", help="Use current Keep CEL/model/parser dependencies too")
    sub.add_parser("inventory")
    args = parser.parse_args()
    if args.command == "inventory":
        print(parameter_inventory(), end="")
        return
    try:
        Draft202012Validator.check_schema(SCHEMA)
        result = validate_bundle(read_yaml(args.bundle), args.bundle.parent, args.tenant, args.compatibility)
    except ContractError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
