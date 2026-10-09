"""IaC derived event fields and presentation, separate from source/operator state."""

import copy
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from urllib.parse import quote, urlsplit

import celpy
from sqlmodel import select

from keep.api.core.incident_configuration import active_configuration
from keep.api.core.incident_contract import ContractError, require, validate_link_url


FIELDS = ("cluster", "environment", "namespace", "kind", "resource", "workload", "service", "routing_level")
DERIVED = frozenset({"normalized", "normalization", "presentation", "correlation"})
_deferred_fingerprints = ContextVar("keep_deferred_event_fingerprints", default=False)
_VARIABLE = re.compile(r"{{\s*([\w.]+)\s*}}")


def defer_event_fingerprints(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _deferred_fingerprints.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            _deferred_fingerprints.reset(token)
    return wrapped


def fingerprints_deferred():
    return _deferred_fingerprints.get()


@contextmanager
def prepared_fingerprints():
    token = _deferred_fingerprints.set(False)
    try:
        yield
    finally:
        _deferred_fingerprints.reset(token)


def path_value(data, path):
    for simple, quoted in re.findall(r'(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)|\["([^"\\]+)"\]', path):
        if not isinstance(data, dict):
            return None
        data = data.get(simple or quoted)
    return data


def validate_presentations(bundle):
    for item in bundle.get("presentations", []):
        collections = {"incident.collections." + collection["id"] for collection in item.get("collections", [])}
        paths = [path for key in ("title", "description") for path in _VARIABLE.findall(item[key])]
        paths += [field["path"] for field in item.get("fields", [])]
        paths += [path for link in item.get("links", []) for path in _VARIABLE.findall(link["url_template"])]
        for path in paths:
            require(path == "keep_url" or path in {"incident." + field for field in (
                    "id", "status", "severity", "team_id", "assignee", "alerts_count", "name", "summary", "episode", "revision", "start_time", "end_time")}
                    or path in {"incident.automation." + field for field in ("level", "ack_breached", "ack_deadline_at", "origin", "episode")}
                    or path in {"incident.flapping.active", "incident.flapping.transition_count"}
                    or path in collections or path in {"normalized." + field for field in FIELDS}, "presentations", "unsupported presentation field: " + path)


def source_content(snapshot, events):
    """Bounded, configured display data. It never changes normalized identity."""
    result = {}
    for definition in snapshot["bundle"].get("presentations", []):
        if not (definition.get("collections") or definition.get("source_links")):
            continue
        shown = events
        if definition.get("prefer_active_alerts", True):
            shown = [event for event in events if event.get("status") != "resolved"] or events
        collections, links = {}, []
        for item in definition.get("collections", []):
            values = set()
            for event in shown:
                for path in item["sources"]:
                    value = path_value(event, path)
                    if path.startswith("normalized.") and not known_normalized_field(event, path):
                        continue
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        values.add(str(value).strip())
                        break
            limit, chars = item.get("max_items", 20), item.get("max_chars", 2048)
            text = "\n".join(sorted(values)[:limit])
            if len(values) > limit:
                text += f"\n… (+{len(values) - limit} more)"
            if len(text) > chars:
                text = text[:chars - 16] + "\n… (truncated)"
            collections[item["id"]] = {"value": text or None, "count": len(values), "sources": item["sources"]}
        for item in definition.get("source_links", []):
            urls = set()
            for event in shown:
                for path in item["sources"]:
                    value = path_value(event, path)
                    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
                        continue
                    try:
                        validate_link_url(value.strip(), "presentation.source_links")
                    except ContractError:
                        continue
                    url = quote(value.strip(), safe=":/?&=#%+,-._~")
                    if len(url) > 2048:
                        continue
                    urls.add(url)
                    break
            urls = sorted(urls)
            for index, url in enumerate(urls[:item.get("max_items", 20)]):
                label = item["label"] + (f" ({index + 1})" if len(urls) > 1 else "")
                links.append({"label": label, "url": url})
        result[definition["id"]] = {"collections": collections, "links": links[:definition.get("max_links", 50)]}
    return result


def known_normalized_field(payload, path):
    if not path.startswith("normalized."):
        return True
    metadata = (payload.get("normalization") or {}).get("fields", {}).get(path.split(".")[1], {})
    return metadata.get("known") is True and path_value(payload, path) is not None


def normalize_event(tenant_id, event, *, projection=None, snapshot=None):
    """Caller supplies canonical team; all incoming claims of derived data are replaced."""
    for key in DERIVED:
        setattr(event, key, None)
    snapshot = snapshot if snapshot is not None else active_configuration(tenant_id)
    if not snapshot:
        return event
    payload = (projection or event).dict()
    for key in DERIVED:
        payload.pop(key, None)
    payload["team_id"] = event.team_id
    for rule in sorted(snapshot["bundle"].get("normalization", []), key=lambda item: item["priority"], reverse=True):
        if event.team_id not in rule["team_ids"]:
            continue
        try:
            environment = celpy.Environment()
            result = environment.program(environment.compile(rule["match"])).evaluate(celpy.json_to_cel(payload))
            matched = isinstance(result, (bool, celpy.celtypes.BoolType)) and bool(result)
        except Exception:
            matched = False
        if not matched:
            continue
        values, provenance = {}, {}
        for field in rule["fields"]:
            value, source, reason = None, None, "missing_source"
            for source_path in field["sources"]:
                candidate = path_value(payload, source_path)
                if candidate is None or candidate == "":
                    continue
                source = source_path
                if not isinstance(candidate, str):
                    reason = "invalid_type"
                    break
                value = candidate.strip()
                if value:
                    break
                source = None
            method = "source"
            if value and field.get("extract"):
                match = re.search(field["extract"]["pattern"], value)
                value = match.group(field["extract"].get("group", 1)) if match else None
                method, reason = "regex", "regex_no_match"
            known = bool(value)
            if not known:
                missing = field["missing"]
                value = missing.get("value") if missing["mode"] == "literal" else None
                method = "literal" if value else "unknown"
            values[field["name"]] = value
            provenance[field["name"]] = {"source": source, "method": method, "known": known,
                                          "reason": None if known else reason}
        event.normalized = values
        event.normalization = {"policy_id": rule["id"], "config_digest": snapshot["digest"],
                               "presentation_ref": rule.get("presentation_ref"), "fields": provenance}
        content = source_content(snapshot, [{**payload, "normalized": values, "normalization": event.normalization}])
        if content:
            event.normalization["source_content"] = content
        event.presentation = render_presentation(tenant_id, values, event.normalization, snapshot=snapshot)
        break
    return event


def deduplication_payload(event):
    payload = copy.deepcopy(event.to_ingestion_dict())
    # Configuration/provenance/template changes cannot manufacture duplicate state transitions.
    payload.pop("normalization", None)
    payload.pop("presentation", None)
    payload.pop("correlation", None)
    return payload


def render_presentation(tenant_id, normalized, metadata, incident=None, *, snapshot=None, definition=None, extra=None):
    snapshot = snapshot if snapshot is not None else active_configuration(tenant_id)
    if not snapshot or not metadata:
        return None
    reference = metadata.get("presentation_ref")
    definition = definition or next((item for item in snapshot["bundle"].get("presentations", []) if item["id"] == reference), None)
    if not definition:
        return None
    content = metadata.get("source_content", {}).get(reference, {})
    collections = content.get("collections", {})
    context = {**(extra or {}), "normalized": normalized, "incident": {**(incident or {}), "collections": {
        name: item["value"] for name, item in collections.items()}}, "keep_url": snapshot["bundle"]["keep_url"].rstrip("/")}
    missing = set()

    def render(template, *, url=False):
        def substitute(match):
            path = match.group(1)
            value = path_value(context, path)
            if value is None or value == "" or (path.startswith("normalized.") and not known_normalized_field(
                    {"normalized": normalized, "normalization": metadata}, path)):
                missing.add(path)
                if url:
                    return ""
            if value is None or value == "":
                value = "unknown"
            # Only the configured base URL is kept as a URL; event values are path/query components.
            return quote(str(value), safe="") if url and path != "keep_url" else str(value)
        return _VARIABLE.sub(substitute, template)

    title, description = render(definition["title"]), render(definition["description"])
    display = []
    for item in sorted(definition.get("fields", []), key=lambda field: field.get("order", 0)):
        if "when" in item:
            from keep.api.core.notification_policies import condition
            try:
                if not condition(item["when"], context):
                    continue
            except Exception:
                continue
        path = item["path"]
        value = path_value(context, path)
        known = value is not None and (not path.startswith("normalized.") or known_normalized_field(
            {"normalized": normalized, "normalization": metadata}, path))
        if not known:
            missing.add(path)
        display.append({"path": path, "label": item["label"], "value": value, "known": known,
                        "source": ", ".join(collections.get(path.split(".")[-1], {}).get("sources", [])) if path.startswith("incident.collections.") else
                        metadata.get("fields", {}).get(path.split(".")[-1], {}).get("source") or
                        ", ".join(metadata.get("fields", {}).get(path.split(".")[-1], {}).get("sources", [])) or None})
    links = []
    for item in definition.get("links", []):
        required = _VARIABLE.findall(item["url_template"])
        if any(path_value(context, path) in (None, "") or (path.startswith("normalized.") and not known_normalized_field(
                {"normalized": normalized, "normalization": metadata}, path)) for path in required):
            continue
        url = render(item["url_template"], url=True)
        parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            links.append({"label": item["label"], "url": url})
    links += content.get("links", [])
    distinct_links = {}
    for link in links:
        distinct_links.setdefault(link["url"], link)
    links = list(distinct_links.values())[:definition.get("max_links", 50)]
    severity = (incident or {}).get("severity")
    return {"id": reference, "config_digest": snapshot["digest"], "title": title, "description": description,
            "fields": display, "links": links, "missing_fields": sorted(missing),
            "severity_color": definition.get("status_colors", {}).get((incident or {}).get("status")) or
                              definition.get("severity_colors", {}).get(severity),
            **({"footer": render(definition["footer"])} if "footer" in definition else {}),
            **({"tags": [render(tag) for tag in definition["tags"]]} if "tags" in definition else {})}


def refresh_incident_presentation(tenant_id, incident, session, *, events=None, snapshot=None):
    """Rebuild from current linked source snapshots, not fingerprint enrichments."""
    from keep.api.models.db.alert import Alert, LastAlert, LastAlertToIncident, NULL_FOR_DELETED_AT

    snapshot = snapshot if snapshot is not None else active_configuration(tenant_id)
    if not incident.normalization_context and not (snapshot and snapshot["bundle"].get("normalization")):
        return
    if events is None:
        events = session.exec(select(Alert.event).select_from(LastAlert).join(
            LastAlertToIncident, (LastAlertToIncident.tenant_id == LastAlert.tenant_id)
            & (LastAlertToIncident.fingerprint == LastAlert.fingerprint)).join(Alert, LastAlert.alert_id == Alert.id).where(
                LastAlert.tenant_id == tenant_id, Alert.tenant_id == tenant_id,
                LastAlertToIncident.tenant_id == tenant_id, LastAlertToIncident.incident_id == incident.id,
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT, Alert.team_id == incident.team_id)).all()
    if not any(event.get("normalization") for event in events):
        if incident.normalization_context:
            incident.normalization_context = None
            incident.generated_name = None
            incident.generated_summary = None
        return
    values, provenance = {}, {}
    for name in FIELDS:
        sources = [(event.get("normalization") or {}).get("fields", {}).get(name, {}) for event in events]
        distinct = {(event.get("normalized") or {}).get(name) for event in events}
        known = len(distinct) == 1 and None not in distinct and all(source.get("known") for source in sources)
        values[name] = next(iter(distinct)) if len(distinct) == 1 else None
        provenance[name] = {"known": known, "method": "aggregate", "source": None,
                            "sources": sorted({source["source"] for source in sources if source.get("source")}),
                            "reason": None if known else "multiple_values" if len(distinct) > 1 else "incomplete_sources"}
    references = {(event.get("normalization") or {}).get("presentation_ref") for event in events}
    metadata = {"presentation_ref": next(iter(references)) if len(references) == 1 else None, "fields": provenance,
                "policy_ids": sorted({event["normalization"]["policy_id"] for event in events if event.get("normalization")}),
                "config_digests": sorted({event["normalization"]["config_digest"] for event in events if event.get("normalization")})}
    if incident.correlation_context and incident.correlation_context["team_id"] == incident.team_id:
        metadata["presentation_ref"] = incident.correlation_context["policy"]["presentation_ref"]
    if snapshot:
        metadata["presentation_digest"] = snapshot["digest"]
        content = source_content(snapshot, events)
        if content:
            metadata["source_content"] = content
    incident.normalization_context = {"team_id": incident.team_id, "normalized": values, "normalization": metadata}
    from keep.api.utils.alert_utils import extract_service_from_alert
    incident.affected_services = sorted({service for event in events if (service := extract_service_from_alert(event))})
    projection = project_incident(tenant_id, incident, snapshot=snapshot)
    if projection.get("presentation"):
        incident.generated_name = projection["presentation"]["title"]
        incident.generated_summary = projection["presentation"]["description"]
    session.add(incident)


def presentation_incident(incident, *, now=None):
    """Public canonical template values, shared by the UI and ready notifications."""
    from keep.api.models.db.incident import IncidentSeverity
    from keep.api.core.incident_lifecycle import project
    from keep.api.core.incident_automation import project as automation_project
    from keep.api.core.incident_lifecycle import utc
    from keep.api.models.silence import utc_string
    lifecycle = project(incident, now=now) or {}
    start = lifecycle.get("episode_start") or incident.creation_time
    end = (lifecycle.get("resolved_at") or incident.end_time) if incident.status == "resolved" else None
    return {"id": str(incident.id), "team_id": incident.team_id, "status": incident.status,
        "severity": IncidentSeverity.from_number(incident.severity).value, "assignee": incident.assignee,
        "alerts_count": incident.alerts_count, "name": incident.user_generated_name or incident.generated_name or "Incident",
        "summary": incident.user_summary or incident.generated_summary or "Incident update",
        "start_time": utc_string(utc(start)),
        "end_time": utc_string(utc(end)) if end else None,
        "episode": lifecycle.get("episode", 1), "revision": lifecycle.get("revision", 0),
        "flapping": lifecycle.get("flapping"), "automation": automation_project(incident)}


def project_incident(tenant_id, incident, *, snapshot=None):
    context = incident.normalization_context or {}
    if not context:
        return {"normalized": None, "normalization": None, "presentation": None, "generated_name": incident.generated_name}
    if context["team_id"] != incident.team_id:
        return {"normalized": None, "normalization": None, "presentation": None, "generated_name": None, "generated_summary": None}
    presentation = render_presentation(tenant_id, context["normalized"], context["normalization"],
                                       presentation_incident(incident), snapshot=snapshot)
    result = {"normalized": copy.deepcopy(context["normalized"]), "normalization": copy.deepcopy(context["normalization"]), "presentation": presentation,
              "generated_name": presentation["title"] if presentation else incident.generated_name}
    if presentation:
        result["generated_summary"] = presentation["description"]
    return result
