"""Durable correlation keys and explanations; no regrouping of existing history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy_utils import UUIDType

revision = "e5d04b9ca716"
down_revision = "c27f8d21b409"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("incident", sa.Column("correlation_context", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("alert", sa.Column("correlation_context", sa.JSON(none_as_null=True), nullable=True))
    op.create_table("incidentcorrelationgroup",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String(), nullable=True),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("incident_id", UUIDType(binary=False), sa.ForeignKey("incident.id", ondelete="SET NULL"), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True))
    op.create_index("ix_incidentcorrelationgroup_tenant_id", "incidentcorrelationgroup", ["tenant_id"])


def downgrade():
    connection = op.get_bind()
    for table in ("incident", "alert"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} WHERE correlation_context IS NOT NULL LIMIT 1")).first():
            raise RuntimeError("Correlation history exists; export/review before downgrade")
    if connection.execute(sa.text("SELECT 1 FROM incidentcorrelationgroup LIMIT 1")).first():
        raise RuntimeError("Correlation groups exist; export/review before downgrade")
    op.drop_table("incidentcorrelationgroup")
    op.drop_column("alert", "correlation_context")
    op.drop_column("incident", "correlation_context")
