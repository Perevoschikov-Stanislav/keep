"""Durable notification deliveries and shared transport rate budgets.

Revision ID: 9d6a8f3b5e27
Revises: 8c5f7e2a4d16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.dialects.mysql import DATETIME

revision = "9d6a8f3b5e27"
down_revision = "8c5f7e2a4d16"
branch_labels = None
depends_on = None

TIME = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql").with_variant(DATETIME2(precision=6), "mssql")


def upgrade():
    op.create_table("notificationdelivery",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String(256), nullable=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("subscriber_id", sa.String(128), nullable=False),
        sa.Column("destination_id", sa.String(128), nullable=False),
        sa.Column("transport_id", sa.String(128), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", TIME, nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", TIME, nullable=True),
        sa.Column("delivered_at", TIME, nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint("tenant_id", "event_id", "subscriber_id", "destination_id",
                            name="uq_notificationdelivery_receiver"),
    )
    op.create_index("ix_notificationdelivery_due", "notificationdelivery",
                    ["tenant_id", "state", "available_at", "lease_until"])
    op.create_index("ix_notificationdelivery_team", "notificationdelivery", ["tenant_id", "team_id", "created_at", "id"])
    op.create_table("notificationtransportbudget",
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("transport_id", sa.String(128), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
    )


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify that the outbox is empty")
    table = sa.table("notificationdelivery", sa.column("id"))
    if op.get_bind().execute(sa.select(table.c.id).limit(1)).first() is not None:
        raise RuntimeError("Notification delivery history exists: keep additive tables during application rollback")
    op.drop_table("notificationtransportbudget")
    op.drop_table("notificationdelivery")
