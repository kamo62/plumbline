"""Versioned provider snapshots and field-scoped collection checkpoints."""
import sqlalchemy as sa


def upgrade(op):
    op.create_table(
        "evidence_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("connection_id", sa.String(256), nullable=False, index=True),
        sa.Column("window_start", sa.String(32), nullable=False),
        sa.Column("window_end", sa.String(32), nullable=False),
        sa.Column("as_of", sa.String(32), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "evidence_checkpoints",
        sa.Column("connection_id", sa.String(256), primary_key=True),
        sa.Column("project_id", sa.String(256), primary_key=True),
        sa.Column("repository_id", sa.String(256), primary_key=True),
        sa.Column("entity_type", sa.String(16), primary_key=True),
        sa.Column("entity_id", sa.String(256), primary_key=True),
        sa.Column("field", sa.String(128), primary_key=True),
        sa.Column("window_end", sa.String(32), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("evidence_snapshots.id"), nullable=False),
    )
