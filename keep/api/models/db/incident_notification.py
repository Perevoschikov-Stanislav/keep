"""Bindings, scan cursor and command receipts; deliveries use the shared queue."""

from datetime import datetime
from uuid import UUID
from sqlmodel import SQLModel, Field, Column, JSON


class IncidentNotificationBinding(SQLModel, table=True):
    id: str = Field(primary_key=True, max_length=64)
    tenant_id: str = Field(foreign_key="tenant.id", index=True)
    team_id: str | None = Field(default=None)
    incident_id: UUID = Field(index=True)
    destination_id: str = Field(max_length=128)
    transport_id: str = Field(max_length=128)
    external_id: str | None = Field(default=None, max_length=512)
    confirmed_revision: int = Field(default=0)
    active_delivery_id: UUID | None = Field(default=None)
    lease_token: UUID | None = Field(default=None)
    lease_until: datetime | None = Field(default=None)
    uncertain: bool = Field(default=False)


class IncidentNotificationCursor(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True)
    incident_id: UUID | None = Field(default=None)


class IncidentIntegrationCommand(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True)
    actor_scope: str = Field(primary_key=True, max_length=64)
    client_request_id: UUID = Field(primary_key=True)
    incident_id: UUID = Field(index=True)
    command_hash: str = Field(max_length=64)
    actor: dict = Field(sa_column=Column(JSON, nullable=False))
    origin: str = Field(max_length=128)
    response: dict = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime
