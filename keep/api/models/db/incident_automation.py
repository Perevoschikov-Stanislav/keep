"""Business operations for incident automation, not a transport delivery queue."""

from datetime import datetime
from uuid import UUID

from sqlalchemy_utils import UUIDType
from sqlmodel import JSON, Column, Field, Index, SQLModel


class IncidentAutomationOperation(SQLModel, table=True):
    __table_args__ = (Index("ix_incidentautomationoperation_status_due", "status", "due_at"),)

    id: str = Field(primary_key=True, max_length=64)
    tenant_id: str = Field(foreign_key="tenant.id", index=True)
    team_id: str | None = Field(default=None)
    incident_id: UUID = Field(sa_column=Column(UUIDType(binary=False), nullable=False, index=True))
    episode: int
    chain_id: str = Field(max_length=64)
    policy_version: str = Field(max_length=64)
    kind: str = Field(max_length=24)
    level_id: str = Field(max_length=128)
    ordinal: int
    target_kind: str = Field(max_length=24)
    target_ref: str = Field(max_length=128)
    due_at: datetime
    status: str = Field(default="pending", max_length=32)
    context: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    token: str | None = Field(default=None, max_length=36)
    lease_until: datetime | None = None
    effect_started: bool = Field(default=False)
    execution_id: str | None = Field(default=None, max_length=64)
    completed_at: datetime | None = None
