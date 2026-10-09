from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.dialects.mysql import DATETIME
from sqlmodel import JSON, TEXT, Column, Field, Index, SQLModel


# Store naive UTC, retaining the precision promised by the wire contract.
SILENCE_TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql").with_variant(
    DATETIME2(precision=6), "mssql"
)


class Silence(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", max_length=256)
    team_id: str | None = Field(default=None, max_length=256)
    revision: int = Field(default=1)
    selector: dict = Field(sa_column=Column(JSON, nullable=False))
    starts_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    ends_at: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    comment: str = Field(sa_column=Column(TEXT, nullable=False))
    created_by: dict = Field(sa_column=Column(JSON, nullable=False))
    updated_by: dict = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    updated_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    cancelled_at: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    origin: str = Field(max_length=256)
    correlation_id: str | None = Field(default=None, max_length=512)
    last_event_state: str = Field(max_length=16)
    # Server-owned synchronization receipts. Command correlation_id is untrusted metadata.
    external_context: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    __table_args__ = (
        Index("ix_silence_tenant_team_period", "tenant_id", "team_id", "cancelled_at", "starts_at", "ends_at"),
        Index("ix_silence_tenant_created", "tenant_id", "created_at", "id"),
    )


class AlertmanagerReconciliationState(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True, max_length=256)
    source_id: str = Field(primary_key=True, max_length=64)
    lease_token: UUID | None = Field(default=None)
    lease_until: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    next_run_at: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    alert_state: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class SilenceCommand(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True, max_length=256)
    actor_scope: str = Field(primary_key=True, max_length=64)
    client_request_id: UUID = Field(primary_key=True)
    command_hash: str = Field(max_length=64)
    silence_id: UUID = Field(foreign_key="silence.id", index=True)
    http_status: int
    response: dict = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))


class SilenceEvent(SQLModel, table=True):
    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", max_length=256)
    team_id: str | None = Field(default=None, max_length=256)
    silence_id: UUID = Field(foreign_key="silence.id")
    revision: int
    event_type: str = Field(sa_column=Column(String(32), nullable=False))
    occurred_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    payload: dict = Field(sa_column=Column(JSON, nullable=False))

    __table_args__ = (
        UniqueConstraint("tenant_id", "silence_id", "revision", name="uq_silence_event_revision"),
        Index("ix_silenceevent_tenant_team_time", "tenant_id", "team_id", "occurred_at"),
    )


class NotificationDelivery(SQLModel, table=True):
    """Durable per-receiver delivery; only secret references belong in configuration."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", max_length=256)
    team_id: str | None = Field(default=None, max_length=256)
    event_id: UUID
    subscriber_id: str = Field(max_length=128)
    destination_id: str = Field(max_length=128)
    transport_id: str = Field(max_length=128)
    policy_digest: str = Field(max_length=64)
    payload: dict = Field(sa_column=Column(JSON, nullable=False))
    state: str = Field(default="pending", max_length=16)
    attempts: int = Field(default=0)
    available_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    created_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
    lease_token: UUID | None = Field(default=None)
    lease_until: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    delivered_at: datetime | None = Field(default=None, sa_column=Column(SILENCE_TIME))
    last_error_code: str | None = Field(default=None, max_length=64)
    context: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    effect_started: bool = Field(default=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", "subscriber_id", "destination_id",
                         name="uq_notificationdelivery_receiver"),
        Index("ix_notificationdelivery_due", "tenant_id", "state", "available_at", "lease_until"),
        Index("ix_notificationdelivery_team", "tenant_id", "team_id", "created_at", "id"),
    )


class NotificationTransportBudget(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True, max_length=256)
    transport_id: str = Field(primary_key=True, max_length=128)
    tokens: float
    updated_at: datetime = Field(sa_column=Column(SILENCE_TIME, nullable=False))
