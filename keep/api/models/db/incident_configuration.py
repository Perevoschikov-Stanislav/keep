"""IaC configuration only. Operator state and incident history are separate."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import JSON, TEXT, Column, Field, SQLModel, UniqueConstraint
from keep.api.models.db.silence import SILENCE_TIME


class IncidentConfiguration(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True, max_length=256)
    generation: int = 0
    digest: str | None = Field(default=None, max_length=64)
    snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    source: str | None = Field(default=None, sa_column=Column(TEXT))
    updated_by: str | None = Field(default=None, max_length=256)
    updated_at: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))


class IncidentConfigurationVersion(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("tenant_id", "generation", name="uq_incidentconfigurationversion_generation"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", index=True, max_length=256)
    generation: int
    digest: str = Field(max_length=64)
    snapshot: dict = Field(sa_column=Column(JSON, nullable=False))
    source: str = Field(sa_column=Column(TEXT, nullable=False))
    applied_by: str = Field(max_length=256)
    applied_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))


class ManagedIncidentResource(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "target_id", name="uq_managedincidentresource_target"),)
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True, max_length=256)
    kind: str = Field(max_length=32, primary_key=True)
    logical_id: str = Field(max_length=256, primary_key=True)
    bundle_id: str = Field(max_length=128)
    target_id: str | None = Field(default=None, max_length=256)
    digest: str = Field(max_length=64)
    revision: str = Field(max_length=128)
    generation: int
    deleted: bool = False
