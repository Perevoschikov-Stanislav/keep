"""Add legacy import receipts; keep existing incident/operator/history rows."""

from alembic import op
import sqlalchemy as sa

revision = "d39b72e4a610"
down_revision = "a46c093ef271"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("legacyincidentimport",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("root_id", sa.String(36), primary_key=True),
        sa.Column("migration_id", sa.String(128), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("imported_by", sa.String(256), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM legacyincidentimport LIMIT 1")).first():
        raise RuntimeError("Legacy import receipts exist; export/review before downgrade")
    op.drop_table("legacyincidentimport")
