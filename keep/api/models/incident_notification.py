"""Strict v1 commands: roles and operator identity are never accepted in a body."""

from typing import Literal
from uuid import UUID
from pydantic import BaseModel, root_validator
from keep.api.core.incident_contract import validate_shape


class IncidentCommand(BaseModel):
    schema_version: Literal[1]
    client_request_id: UUID
    incident_id: UUID
    expected_revision: int
    command: Literal["ack", "unack", "resolve", "assign"]
    correlation_id: str | None
    assignee: str | None = None

    @root_validator(pre=True)
    def wire(cls, values):
        validate_shape("IncidentCommand", values, "command")
        return values

    class Config:
        extra = "forbid"


class DeliveryReceipt(BaseModel):
    schema_version: Literal[1]
    notification_id: UUID
    destination_ref: str
    status: Literal["delivered", "unknown", "failed"]
    external_id: str | None
    delivered_revision: int | None

    @root_validator(pre=True)
    def wire(cls, values):
        validate_shape("DeliveryReceipt", values, "receipt")
        return values

    class Config:
        extra = "forbid"
