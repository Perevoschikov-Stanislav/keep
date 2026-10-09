"""Startup-owned integration subset of the v1 IaC bundle; no domain-policy apply."""

import hmac
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.core.incident_contract import ContractError, read_yaml, validate_bundle
from keep.identitymanager.team_policy import TeamPolicy, get_team_policy, is_team_scoping_active


class IntegrationConfigurationError(ValueError):
    pass


def secret_value(reference):
    """Resolve references at use time. Neither values nor paths appear in errors."""
    try:
        if reference.startswith("env:"):
            value = os.environ.get(reference[4:])
        elif reference.startswith("file:"):
            value = Path(reference[5:]).read_text(encoding="utf-8").rstrip("\r\n")
        else:
            value = None
        if not value or len(value) > 16384 or any(char in value for char in "\r\n\x00"):
            raise IntegrationConfigurationError("Integration credential unavailable")
        return value
    except (OSError, UnicodeError):
        raise IntegrationConfigurationError("Integration credential unavailable") from None


@dataclass(frozen=True)
class SilenceIntegrations:
    tenant_id: str
    digest: str
    policy: TeamPolicy
    profiles: dict
    clients: dict
    subscribers: dict
    transports: dict
    destinations: dict
    dispatch: dict
    bundle: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path, tenant_id=SINGLE_TENANT_UUID):
        try:
            path = Path(path)
            bundle = read_yaml(path)
            validated = validate_bundle(bundle, path.parent, tenant_id, include_documents=True)
            policy = TeamPolicy(validated["documents"]["access"])
            active = get_team_policy()
            if not is_team_scoping_active() or active is None or policy.__dict__ != active.__dict__:
                raise IntegrationConfigurationError("Integration access must match the active TeamPolicy")
            dispatch = {"scan_interval_seconds": 5, "batch_size": 100,
                        "lease_seconds": 60, "snapshot_page_size": 100, **bundle.get("dispatch", {})}
            def section(name):
                return {item["id"]: item for item in bundle.get(name, [])}
            settings = cls(tenant_id, validated["digest"], policy, section("proof_profiles"),
                           section("service_clients"), section("subscribers"), section("transports"),
                           section("destinations"), dispatch, bundle)
            for subscriber in settings.subscribers.values():
                for reference in subscriber["destination_refs"]:
                    transport = settings.transports[settings.destinations[reference]["transport_ref"]]
                    if transport["adapter_ref"] != "http-json-v1":
                        raise IntegrationConfigurationError("Lifecycle subscriber adapter is not implemented")
            return settings
        except (ContractError, ValueError, TypeError, KeyError) as error:
            if isinstance(error, IntegrationConfigurationError):
                raise
            raise IntegrationConfigurationError("Invalid silence integration configuration") from None

    @classmethod
    def from_snapshot(cls, snapshot):
        bundle = snapshot["bundle"]
        section = lambda name: {item["id"]: item for item in bundle.get(name, [])}
        return cls(bundle["tenant_id"], snapshot["digest"], TeamPolicy(snapshot["documents"]["access"]),
                   section("proof_profiles"), section("service_clients"), section("subscribers"),
                   section("transports"), section("destinations"), bundle["dispatch"], bundle)

    def matching_clients(self, credential):
        if not credential or len(credential) > 16384:
            return []
        matches = []
        for client in self.clients.values():
            if hmac.compare_digest(credential.encode(), secret_value(client["auth_ref"]).encode()):
                matches.append(client)
        return matches

    def authenticate_client(self, credential):
        matches = self.matching_clients(credential)
        # Even different references must not resolve to one ambiguous credential.
        return matches[0] if len(matches) == 1 else None

    def deliveries_for(self, event):
        for subscriber in self.subscribers.values():
            if event.team_id not in subscriber["team_ids"] or event.event_type not in subscriber["event_types"]:
                continue
            for reference in subscriber["destination_refs"]:
                destination = self.destinations[reference]
                if destination["team_id"] == event.team_id:
                    yield subscriber["id"], destination


@lru_cache(maxsize=1)
def _legacy_silence_integrations():
    path = os.environ.get("KEEP_SILENCES_INTEGRATIONS_CONFIG_FILE")
    return SilenceIntegrations.load(path) if path else None


def get_silence_integrations():
    from keep.api.core.incident_configuration import active_configuration

    snapshot = active_configuration()
    return SilenceIntegrations.from_snapshot(snapshot) if snapshot else _legacy_silence_integrations()


get_silence_integrations.cache_clear = _legacy_silence_integrations.cache_clear
