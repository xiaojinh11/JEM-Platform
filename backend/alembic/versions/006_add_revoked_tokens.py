"""Add revoked_tokens table for JWT logout / forced revocation

Revision ID: 006_add_revoked_tokens
Revises: 005
Create Date: 2026-09-23

Background
----------
The previous JWT implementation could only expire tokens passively via
the ``exp`` claim.  ``POST /api/v1/auth/logout`` (added in this
revision) and any admin "force logout" workflow now write a SHA-256
hash of the token into ``revoked_tokens`` so that ``decode_token``
returns ``401`` for the token's remaining lifetime.

We keep ``expires_at`` so a small maintenance task can prune rows that
are guaranteed-dead without ever consulting the JWT itself.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_add_revoked_tokens"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
    )
    op.create_index("ix_revoked_tokens_token_hash", "revoked_tokens", ["token_hash"], unique=True)
    op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_index("ix_revoked_tokens_user_id", table_name="revoked_tokens")
    op.drop_index("ix_revoked_tokens_token_hash", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
