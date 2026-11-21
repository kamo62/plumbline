"""Add immutable diagnostic measurements without changing existing evidence."""
import sqlalchemy as sa

from .models import UUIDString


def upgrade(op):
    # Measurement audit subjects are 64-character hashes. SQLite does not enforce
    # the VARCHAR width declared by the original audit migration.
    if op.get_context().dialect.name == "postgresql":
        op.alter_column("audit_events", "subject_id", existing_type=sa.String(36), type_=sa.String(64))

    op.create_table(
        "metric_measurements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", UUIDString(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("evidence_snapshots.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("parameters_hash", sa.String(64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "parameters_hash", name="uq_metric_measurements_run_parameters"),
    )
