"""Version 1 wire types. Server identity never comes from a command body."""

import re
from datetime import datetime, timezone
from typing import Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, conint, constr, root_validator, validator


Id = constr(strict=True, min_length=1, max_length=256)
Fingerprint = constr(strict=True, min_length=1, max_length=1024)
Comment = constr(strict=True, max_length=4000)
Correlation = constr(strict=True, min_length=1, max_length=512)
Revision = conint(strict=True, ge=1)
State = Literal["scheduled", "active", "expired", "cancelled"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_string(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class UtcTime(str):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
        ):
            raise ValueError("Expected a UTC timestamp ending in Z")
        parsed = datetime.fromisoformat(value[:-1])
        return cls(utc_string(parsed))

    @classmethod
    def __modify_schema__(cls, schema):
        schema.update(type="string", format="date-time")


class StrictInput(BaseModel):
    class Config:
        extra = "forbid"


class AlertSelector(StrictInput):
    kind: Literal["alert"]
    fingerprints: list[Fingerprint] = Field(..., min_items=1, max_items=1000)

    @validator("fingerprints")
    def unique_fingerprints(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate fingerprint")
        return sorted(value)


class IncidentSelector(StrictInput):
    kind: Literal["incident"]
    incident_ids: list[UUID] = Field(..., min_items=1, max_items=1000)

    @validator("incident_ids")
    def unique_incidents(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate incident")
        return sorted(value, key=str)


class FilterSelector(StrictInput):
    kind: Literal["filter"]
    cel: constr(strict=True, min_length=1, max_length=8192)


Selector = Union[AlertSelector, IncidentSelector, FilterSelector]


class VersionedInput(StrictInput):
    schema_version: Literal[1]

    @validator("schema_version", pre=True)
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported schema version")
        return value


class CreateSilenceCommand(VersionedInput):
    client_request_id: UUID
    team_id: Id | None = Field(...)
    selector: Selector = Field(..., discriminator="kind")
    starts_at: UtcTime | None = Field(...)
    ends_at: UtcTime | None = Field(...)
    comment: Comment
    correlation_id: Correlation | None = Field(...)


class SilenceChanges(StrictInput):
    selector: Selector | None = Field(None, discriminator="kind")
    starts_at: UtcTime | None = None
    ends_at: UtcTime | None = None
    comment: Comment | None = None

    @root_validator(pre=True)
    def nonempty_and_nonnullable_fields(cls, values):
        if not values:
            raise ValueError("At least one change is required")
        for field in ("selector", "starts_at", "comment"):
            if field in values and values[field] is None:
                raise ValueError(f"{field} cannot be null")
        return values


class UpdateSilenceCommand(VersionedInput):
    client_request_id: UUID
    expected_revision: Revision
    changes: SilenceChanges
    correlation_id: Correlation | None = Field(...)


class CancelSilenceCommand(VersionedInput):
    client_request_id: UUID
    expected_revision: Revision
    reason: constr(strict=True, min_length=1, max_length=4000)
    correlation_id: Correlation | None = Field(...)


class AlertTarget(StrictInput):
    kind: Literal["alert"]
    fingerprint: Fingerprint


class IncidentTarget(StrictInput):
    kind: Literal["incident"]
    incident_id: UUID


Target = Union[AlertTarget, IncidentTarget]


class EffectiveSilenceQuery(VersionedInput):
    targets: list[Target] = Field(..., min_items=1, max_items=1000)

    @validator("targets")
    def unique_targets(cls, value):
        keys = [target.json(sort_keys=True) for target in value]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate target")
        return value


class SilenceActor(BaseModel):
    kind: Literal["user", "service"]
    subject: str
    issuer: str | None
    display_name: str


class SilenceDto(BaseModel):
    schema_version: Literal[1] = 1
    id: UUID
    revision: int
    tenant_id: str
    team_id: str | None
    selector: Selector = Field(..., discriminator="kind")
    starts_at: str
    ends_at: str | None
    comment: str
    created_by: SilenceActor
    updated_by: SilenceActor
    created_at: str
    updated_at: str
    cancelled_at: str | None
    origin: str
    correlation_id: str | None
    state: State
    evaluated_at: str
    read_only: bool = False
    synchronization: list[dict] = Field(default_factory=list)


class SilenceMutationResponse(BaseModel):
    schema_version: Literal[1] = 1
    client_request_id: UUID
    replayed: bool
    result: SilenceDto


class SilenceListResponse(BaseModel):
    schema_version: Literal[1] = 1
    evaluated_at: str
    items: list[SilenceDto]
    next_cursor: str | None


class SilenceReason(BaseModel):
    silence_id: UUID
    revision: int
    via: Literal["fingerprint", "filter", "incident"]
    incident_id: UUID | None
    ends_at: str | None
    read_only: bool = False


class EffectiveSilenceItem(BaseModel):
    target: Target
    silenced: bool
    coverage: Literal["none", "partial", "full"]
    silenced_until: str | None
    reasons: list[SilenceReason]
    total_alerts: int
    silenced_alerts: int


class EffectiveSilenceResponse(BaseModel):
    schema_version: Literal[1] = 1
    evaluated_at: str
    items: list[EffectiveSilenceItem]


class SilenceMetadata(BaseModel):
    evaluated_at: str
    silenced: bool
    coverage: Literal["none", "partial", "full"]
    silenced_until: str | None
    reasons: list[SilenceReason]
    total_alerts: int
    silenced_alerts: int
