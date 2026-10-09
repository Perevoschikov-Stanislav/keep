"""Extend the shared notification queue and persist bindings/command receipts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy_utils import UUIDType

revision = "a46c093ef271"
down_revision = "6b2fda31c890"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("incident", sa.Column("notification_context", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("notificationdelivery", sa.Column("context", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("notificationdelivery", sa.Column("effect_started", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("incidentnotificationbinding",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("team_id", sa.String()), sa.Column("incident_id", UUIDType(binary=False), nullable=False),
        sa.Column("destination_id", sa.String(128), nullable=False), sa.Column("transport_id", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(512)), sa.Column("confirmed_revision", sa.Integer(), nullable=False),
        sa.Column("active_delivery_id", UUIDType(binary=False)), sa.Column("lease_token", UUIDType(binary=False)),
        sa.Column("lease_until", sa.DateTime()), sa.Column("uncertain", sa.Boolean(), nullable=False))
    op.create_index("ix_incidentnotificationbinding_tenant_id", "incidentnotificationbinding", ["tenant_id"])
    op.create_index("ix_incidentnotificationbinding_incident_id", "incidentnotificationbinding", ["incident_id"])
    op.create_table("incidentnotificationcursor",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("incident_id", UUIDType(binary=False)))
    op.create_table("incidentintegrationcommand",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("actor_scope", sa.String(64), primary_key=True),
        sa.Column("client_request_id", UUIDType(binary=False), primary_key=True),
        sa.Column("incident_id", UUIDType(binary=False), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False), sa.Column("origin", sa.String(128), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_incidentintegrationcommand_incident_id", "incidentintegrationcommand", ["incident_id"])


def downgrade():
    connection = op.get_bind()
    for table in ("incidentnotificationbinding", "incidentnotificationcursor", "incidentintegrationcommand"):
        if connection.execute(sa.text("SELECT 1 FROM " + table + " LIMIT 1")).first():
            raise RuntimeError("Notification state exists; export/review before downgrade")
    if connection.execute(sa.text("SELECT 1 FROM incident WHERE notification_context IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Notification state exists; export/review before downgrade")
    # Silence deliveries must survive. Their legacy context is the empty object.
    if connection.execute(sa.text("SELECT 1 FROM notificationdelivery WHERE effect_started = true OR CAST(context AS TEXT) <> '{}' LIMIT 1")).first():
        raise RuntimeError("Notification state exists; export/review before downgrade")
    for table in ("incidentintegrationcommand", "incidentnotificationcursor", "incidentnotificationbinding"):
        op.drop_table(table)
    op.drop_column("notificationdelivery", "effect_started")
    op.drop_column("notificationdelivery", "context")
    op.drop_column("incident", "notification_context")
