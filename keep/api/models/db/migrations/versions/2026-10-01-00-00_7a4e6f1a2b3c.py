"""Add team ownership to alerts and incidents.

Revision ID: 7a4e6f1a2b3c
Revises: 67ff7efffed4
"""

import sqlalchemy as sa
from alembic import op

revision = "7a4e6f1a2b3c"
down_revision = "67ff7efffed4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lastalerttoincident") as batch_op:
        batch_op.create_index(
            "idx_lastalerttoincident_tenant_incident", ["tenant_id", "incident_id"]
        )
    with op.batch_alter_table("alert") as batch_op:
        batch_op.add_column(sa.Column("team_id", sa.String(255), nullable=True))
        batch_op.create_index("ix_alert_team_id", ["team_id"])
    with op.batch_alter_table("incident") as batch_op:
        batch_op.add_column(sa.Column("team_id", sa.String(255), nullable=True))
        batch_op.create_index("ix_incident_team_id", ["team_id"])


def downgrade() -> None:
    with op.batch_alter_table("incident") as batch_op:
        batch_op.drop_index("ix_incident_team_id")
        batch_op.drop_column("team_id")
    with op.batch_alter_table("alert") as batch_op:
        batch_op.drop_index("ix_alert_team_id")
        batch_op.drop_column("team_id")
    with op.batch_alter_table("lastalerttoincident") as batch_op:
        batch_op.drop_index("idx_lastalerttoincident_tenant_incident")
