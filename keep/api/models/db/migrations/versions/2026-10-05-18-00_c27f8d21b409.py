"""Separate generated presentation from manual incident text.

Revision ID: c27f8d21b409
Revises: a83d91ce6f40
"""

from alembic import op
import sqlalchemy as sa

revision = "c27f8d21b409"
down_revision = "a83d91ce6f40"
branch_labels = None
depends_on = None


def upgrade():
    # Legacy automatic and manual names cannot be distinguished reliably.
    op.add_column("incident", sa.Column("generated_name", sa.Text(), nullable=True))
    op.add_column("incident", sa.Column("normalization_context", sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    connection = op.get_bind()
    populated = connection.execute(sa.text(
        "SELECT 1 FROM incident WHERE generated_name IS NOT NULL OR normalization_context IS NOT NULL LIMIT 1"
    )).first()
    if populated:
        raise RuntimeError("Derived presentation exists; export/review before downgrade")
    op.drop_column("incident", "normalization_context")
    op.drop_column("incident", "generated_name")
