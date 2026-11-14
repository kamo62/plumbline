"""Add OIDC identity bindings and single-use login transactions."""
import sqlalchemy as sa


def upgrade(op):
    op.create_table(
        "auth_oidc_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("auth_users.id"), nullable=False, unique=True),
        sa.UniqueConstraint("issuer", "subject"),
    )
    op.create_table(
        "auth_oidc_transactions",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
