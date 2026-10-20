"""add AI telemetry logs table (010)

Adds a`i_telemetry_logs` - one row per LMM HTTP request so we can
build cost / latency dashboards.  Indexes are deliberately minimal:
``created_at` for time-bucketed aggregation, `(user_id, created_at)`
for per-user reports.  No unique constraints - duplicate rows from
concurrent requests are tolerated.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "010_add_ai_telemetry"
down_revision: Union[str, Sequence[str], None] = "009_add_ai_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_telemetry_logs",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="chat"),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="chat"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index("ix_ai_telem_created", "ai_telemetry_logs", ["created_at"], if_not_exists=True)
    op.create_index("ix_ai_telem_user_created", "ai_telemetry_logs", ["user_id", "created_at"], if_not_exists=True)
    op.create_index("ix_ai_telem_total", "ai_telemetry_logs", ["total_tokens"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_ai_telem_total", table_name="ai_telemetry_logs", if_exists=True)
    op.drop_index("ix_ai_telem_user_created", table_name="ai_telemetry_logs", if_exists=True)
    op.drop_index("ix_ai_telem_created", table_name="ai_telemetry_logs", if_exists=True)
    op.drop_table("ai_telemetry_logs", if_exists=True)
