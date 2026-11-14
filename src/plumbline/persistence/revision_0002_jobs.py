"""Add the durable job queue to the baseline schema."""
import sqlalchemy as sa
from .models import UUIDString


def upgrade(op):
    op.create_table(
        "jobs", sa.Column("id", UUIDString(), primary_key=True),
        sa.Column("run_id", UUIDString(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("config", sa.JSON(), nullable=False), sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("owner", sa.String(36)), sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)), sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
