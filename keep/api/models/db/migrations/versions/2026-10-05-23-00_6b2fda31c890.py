"""Persist SLA state and uniquely keyed incident automation operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy_utils import UUIDType

revision = "6b2fda31c890"
down_revision = "f38a21d67b04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("incident", sa.Column("automation_context", sa.JSON(none_as_null=True), nullable=True))
    op.create_table("incidentautomationoperation",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String(), nullable=True),
        sa.Column("incident_id", UUIDType(binary=False), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("chain_id", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("level_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(24), nullable=False),
        sa.Column("target_ref", sa.String(128), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("token", sa.String(36)), sa.Column("lease_until", sa.DateTime()),
        sa.Column("effect_started", sa.Boolean(), nullable=False),
        sa.Column("execution_id", sa.String(64)), sa.Column("completed_at", sa.DateTime()))
    for name, columns in (("ix_incidentautomationoperation_tenant_id", ["tenant_id"]),
                          ("ix_incidentautomationoperation_incident_id", ["incident_id"]),
                          ("ix_incidentautomationoperation_status_due", ["status", "due_at"])):
        op.create_index(name, "incidentautomationoperation", columns)


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM incidentautomationoperation LIMIT 1")).first() or connection.execute(
            sa.text("SELECT 1 FROM incident WHERE automation_context IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Automation state exists; export/review before downgrade")
    op.drop_table("incidentautomationoperation")
    op.drop_column("incident", "automation_context")
