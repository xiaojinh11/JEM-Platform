"""add AI conversation memory + user preference tables (009)

Adds two tables that back the short-term and long-term memory tiers
for the NAW AI agent.

* ``ai_conversation_memory`` — session-scoped (user_id, session_id,
  scope, key) → JSON value.  Auto-cleared after 7 days of inactivity
  by a future cleanup task; the read path also ignores rows older than
  that regardless.
* ``ai_user_preferences`` — one row per user, holds vendor defaults,
  alert keywords, custom prompt fragments.

Indexes are deliberately minimal: the read hot-path is
``(user_id, session_id, scope, key)`` and that's already a primary
scan on the small per-session row count.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_add_ai_memory"
# Follows the merged head so this works on both the linear chain and the
# branch that includes be2c1929b6cc.
down_revision: Union[str, Sequence[str], None] = "be2c1929b6cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_conversation_memory",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="chat"),
        sa.Column("memory_type", sa.String(length=32), nullable=False, server_default="device_context"),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="tool"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_ai_conv_memory_user_session",
        "ai_conversation_memory",
        ["user_id", "session_id", "scope", "key"],
        unique=True,
        if_not_exists=True,
    )

    op.create_table(
        "ai_user_preferences",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("default_group", sa.String(length=64), nullable=True),
        sa.Column("preferred_vendors", sa.JSON(), nullable=True),
        sa.Column("alert_keywords", sa.JSON(), nullable=True),
        sa.Column("custom_prompts", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_ai_user_preferences_user_id"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("ai_user_preferences", if_exists=True)
    op.drop_index(
        "ix_ai_conv_memory_user_session",
        table_name="ai_conversation_memory",
        if_exists=True,
    )
    op.drop_table("ai_conversation_memory", if_exists=True)
