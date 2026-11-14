"""Add local accounts, revocable credentials and security audit records."""
import sqlalchemy as sa


def upgrade(op):
    op.create_table(
        "auth_users", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("force_change", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "auth_sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("auth_users.id"), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "auth_tokens", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("auth_users.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "auth_attempts", sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("until", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "audit_events", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("actor_id", sa.String(36)), sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(36)),
    )
