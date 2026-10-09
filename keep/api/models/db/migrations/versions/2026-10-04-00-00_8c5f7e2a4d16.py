"""Persistent silence registry, command receipts and audit.

Revision ID: 8c5f7e2a4d16
Revises: 7a4e6f1a2b3c
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.dialects.mysql import DATETIME

revision = "8c5f7e2a4d16"
down_revision = "7a4e6f1a2b3c"
branch_labels = None
depends_on = None

TIME = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql").with_variant(DATETIME2(precision=6), "mssql")


def upgrade():
    op.create_table("silence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String(256), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("starts_at", TIME, nullable=False), sa.Column("ends_at", TIME, nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_by", sa.JSON(), nullable=False), sa.Column("updated_by", sa.JSON(), nullable=False),
        sa.Column("created_at", TIME, nullable=False), sa.Column("updated_at", TIME, nullable=False),
        sa.Column("cancelled_at", TIME, nullable=True),
        sa.Column("origin", sa.String(256), nullable=False),
        sa.Column("correlation_id", sa.String(512), nullable=True),
        sa.Column("last_event_state", sa.String(16), nullable=False),
    )
    op.create_index("ix_silence_tenant_team_period", "silence", ["tenant_id", "team_id", "cancelled_at", "starts_at", "ends_at"])
    op.create_index("ix_silence_tenant_created", "silence", ["tenant_id", "created_at", "id"])
    op.create_table("silencecommand",
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("actor_scope", sa.String(64), primary_key=True),
        sa.Column("client_request_id", sa.Uuid(), primary_key=True),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("silence_id", sa.Uuid(), sa.ForeignKey("silence.id"), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False), sa.Column("created_at", TIME, nullable=False),
    )
    op.create_index("ix_silencecommand_silence_id", "silencecommand", ["silence_id"])
    op.create_table("silenceevent",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String(256), nullable=True),
        sa.Column("silence_id", sa.Uuid(), sa.ForeignKey("silence.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", TIME, nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tenant_id", "silence_id", "revision", name="uq_silence_event_revision"),
    )
    op.create_index("ix_silenceevent_tenant_team_time", "silenceevent", ["tenant_id", "team_id", "occurred_at"])


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify that silence audit is empty")
    connection = op.get_bind()
    for name in ("silenceevent", "silencecommand", "silence"):
        table = sa.table(name, sa.column("tenant_id"))
        if connection.execute(sa.select(table.c.tenant_id).limit(1)).first() is not None:
            raise RuntimeError("Silence data exists: keep the additive tables when rolling back the application")
    op.drop_table("silenceevent")
    op.drop_table("silencecommand")
    op.drop_table("silence")
