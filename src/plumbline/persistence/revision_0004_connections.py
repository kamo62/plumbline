"""Add encrypted provider connections."""
import sqlalchemy as sa


def upgrade(op):
    op.create_table(
        "connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("organisation", sa.String(256)),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("credential_ciphertext", sa.Text()),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("test_status", sa.String(32), nullable=False),
        sa.Column("test_result", sa.JSON()),
    )
