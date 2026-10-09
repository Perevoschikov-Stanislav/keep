"""Versioned atomic incident configuration and stable resource ownership.

Revision ID: a83d91ce6f40
Revises: 9d6a8f3b5e27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.dialects.mysql import DATETIME

revision = "a83d91ce6f40"
down_revision = "9d6a8f3b5e27"
branch_labels = None
depends_on = None
TIME = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql").with_variant(DATETIME2(precision=6), "mssql")


def upgrade():
    op.create_table("incidentconfiguration",
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(256), nullable=True),
        sa.Column("updated_at", TIME, nullable=True))
    op.create_table("incidentconfigurationversion",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("applied_by", sa.String(256), nullable=False),
        sa.Column("applied_at", TIME, nullable=False),
        sa.UniqueConstraint("tenant_id", "generation", name="uq_incidentconfigurationversion_generation"))
    op.create_index("ix_incidentconfigurationversion_tenant_id", "incidentconfigurationversion", ["tenant_id"])
    op.create_table("managedincidentresource",
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("logical_id", sa.String(256), primary_key=True),
        sa.Column("bundle_id", sa.String(128), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "kind", "target_id", name="uq_managedincidentresource_target"))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify that configuration history is empty")
    history = sa.table("incidentconfigurationversion", sa.column("id"))
    if op.get_bind().execute(sa.select(history.c.id).limit(1)).first() is not None:
        raise RuntimeError("Configuration history exists: preserve additive tables during application rollback")
    op.drop_table("managedincidentresource")
    op.drop_table("incidentconfigurationversion")
    op.drop_table("incidentconfiguration")
