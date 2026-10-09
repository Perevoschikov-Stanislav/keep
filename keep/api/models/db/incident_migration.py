"""Immutable receipts and provenance, separate from canonical operator state."""

from datetime import datetime

from sqlmodel import JSON, Column, Field, SQLModel


class LegacyIncidentImport(SQLModel, table=True):
    tenant_id: str = Field(foreign_key="tenant.id", primary_key=True)
    source_id: str = Field(primary_key=True, max_length=128)
    root_id: str = Field(primary_key=True, max_length=36)
    migration_id: str = Field(max_length=128)
    plan_digest: str = Field(max_length=64)
    source_digest: str = Field(max_length=64)
    candidate_digest: str = Field(max_length=64)
    imported_by: str = Field(max_length=256)
    imported_at: datetime
    provenance: dict = Field(sa_column=Column(JSON, nullable=False))
    result: dict = Field(sa_column=Column(JSON, nullable=False))
