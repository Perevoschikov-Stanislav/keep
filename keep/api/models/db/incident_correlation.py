"""One durable serialization point for each scoped correlation key."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy_utils import UUIDType
from sqlmodel import JSON, Column, Field, SQLModel


class IncidentCorrelationGroup(SQLModel, table=True):
    id: str = Field(primary_key=True, max_length=64)
    tenant_id: str = Field(foreign_key="tenant.id", index=True)
    team_id: str | None = Field(default=None)
    rule_id: str
    rule_version: str = Field(max_length=64)
    incident_id: UUID | None = Field(default=None, sa_column=Column(
        UUIDType(binary=False), ForeignKey("incident.id", ondelete="SET NULL"), nullable=True))
    opened_at: datetime | None = None
    lifecycle_state: dict | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
