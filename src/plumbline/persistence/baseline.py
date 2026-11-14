"""Immutable schema for revision plumbline_0001."""
import sqlalchemy as sa
from .models import UUIDString


def upgrade(op):
    def context():
        return [sa.Column(name, sa.String(256), index=True) for name in ("org", "project", "repo")]
    op.create_table(
        "runs", sa.Column("id", UUIDString(), primary_key=True), *context(),
        sa.Column("branch", sa.String(256), index=True),
        sa.Column("commit_sha", sa.String(64), index=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("environment", sa.String(64)), sa.Column("run_label", sa.String(256)),
        sa.Column("tool_version", sa.String(64)), sa.Column("thresholds_version", sa.String(64)),
        sa.Column("config_hash", sa.String(128)), sa.Column("calculation_version", sa.String(64)),
        sa.Column("status", sa.String(32), index=True), sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), index=True), sa.Column("finished_at", sa.DateTime(timezone=True), index=True),
        sa.Column("artifacts_dir", sa.Text()),
    )
    op.create_table(
        "raw_metrics", sa.Column("id", UUIDString(), primary_key=True), *context(),
        sa.Column("run_id", UUIDString(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("source_system", sa.String(64), nullable=False, index=True),
        sa.Column("source_key", sa.String(256), nullable=False, index=True),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("run_id", "source_system", "source_key", name="uq_raw_identity"),
    )
    op.create_table(
        "metrics", sa.Column("id", UUIDString(), primary_key=True), *context(),
        sa.Column("run_id", UUIDString(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("scope", sa.String(256), nullable=False, index=True),
        sa.Column("metric_key", sa.String(256), nullable=False, index=True),
        sa.Column("metric_value", sa.Numeric()), sa.Column("metric_json", sa.JSON()),
        sa.Column("score", sa.Numeric()), sa.Column("status", sa.String(64)),
        sa.Column("calculation_version", sa.String(64)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("run_id", "scope", "metric_key", name="uq_metric_identity"),
    )
