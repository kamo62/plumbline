"""Link each run to its original immutable provider snapshot."""
import sqlalchemy as sa

from .models import UUIDString


def upgrade(op):
    op.create_table(
        "run_evidence",
        sa.Column("run_id", UUIDString(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("evidence_snapshots.id", ondelete="RESTRICT"),
                  nullable=False, index=True),
    )
