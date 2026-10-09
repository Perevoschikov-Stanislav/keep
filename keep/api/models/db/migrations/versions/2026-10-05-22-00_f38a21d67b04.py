"""Persist lifecycle revisions and group flapping state without rewriting history."""

from alembic import op
import sqlalchemy as sa

revision = "f38a21d67b04"
down_revision = "e5d04b9ca716"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("incident", sa.Column("lifecycle_context", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("incidentcorrelationgroup", sa.Column("lifecycle_state", sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    connection = op.get_bind()
    for table, column in (("incident", "lifecycle_context"), ("incidentcorrelationgroup", "lifecycle_state")):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1")).first():
            raise RuntimeError("Lifecycle state exists; export/review before downgrade")
    op.drop_column("incidentcorrelationgroup", "lifecycle_state")
    op.drop_column("incident", "lifecycle_context")
