"""Server-owned Alertmanager receipts and a distributed reconciliation lease."""

from alembic import op
import sqlalchemy as sa

revision = "e73b914a620c"
down_revision = "d39b72e4a610"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("silence", sa.Column("external_context", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("alertmanagerreconciliationstate",
        sa.Column("tenant_id", sa.String(256), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("next_run_at", sa.DateTime()),
        sa.Column("alert_state", sa.JSON(), nullable=False))


def downgrade():
    connection = op.get_bind()
    if (connection.execute(sa.text("SELECT 1 FROM alertmanagerreconciliationstate LIMIT 1")).first()
            or connection.execute(sa.text("SELECT 1 FROM silence WHERE CAST(external_context AS TEXT) <> '{}' LIMIT 1")).first()):
        raise RuntimeError("Reconciliation receipts exist; review external silences before downgrade")
    op.drop_table("alertmanagerreconciliationstate")
    op.drop_column("silence", "external_context")
