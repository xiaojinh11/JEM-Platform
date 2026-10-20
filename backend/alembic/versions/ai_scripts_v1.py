"""add AI script tables and new config columns (dialect-independent, merge point)

Revision ID: ai_scripts_v1
Revises: d4201e9b8fae

Both the stub path (existing DBs at d4201e9b8fae) and the main path
(reaching here via drift_tables_v1) converge at this revision.

Linear chain: drift_tables_v1 -> d4201e9b8fae -> ai_scripts_v1 -> 003 -> 004 -> 005

The ai_scripts and ai_script_runs tables are created with IF NOT EXISTS,
and columns are added via ALTER TABLE ADD COLUMN IF NOT EXISTS, so this
migration is idempotent on both paths.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ai_scripts_v1"
down_revision: Union[str, Sequence[str], None] = "d4201e9b8fae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── New columns on ai_config ─────────────────────────────────
    # batch_alter_table handles SQLite; if_not_exists on columns is not standard,
    # but we use a try/except pattern via op.execute for true idempotency.
    for col_name, col_type, default in [
        ("scripts_enabled", sa.Boolean(), "1"),
        ("multimodal_enabled", sa.Boolean(), "1"),
        ("stream_enabled", sa.Boolean(), "1"),
        ("script_exec_timeout", sa.Integer(), "30"),
        ("script_max_output_kb", sa.Integer(), "512"),
    ]:
        _add_column_idempotent("ai_config", col_name, col_type, default)

    # ── New column on ai_chat_messages ───────────────────────────
    _add_column_idempotent("ai_chat_messages", "attachments", sa.JSON(), None)

    # ── ai_scripts table ──────────────────────────────────────────
    op.create_table(
        "ai_scripts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_code", sa.Text(), nullable=False, server_default=""),
        sa.Column("inputs_schema", sa.JSON(), nullable=True),
        sa.Column("tags", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_ok", sa.Boolean(), nullable=True),
        sa.Column("last_run_message", sa.Text(), nullable=True),
        sa.Column("total_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_run_seconds", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        if_not_exists=True,
    )
    op.create_index("ix_ai_scripts_name", "ai_scripts", ["name"], if_not_exists=True)

    # ── ai_script_runs table ─────────────────────────────────────
    op.create_table(
        "ai_script_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("script_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("invoked_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("return_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["script_id"], ["ai_scripts.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    op.create_index("ix_ai_script_runs_script_id", "ai_script_runs", ["script_id"], if_not_exists=True)
    op.create_index("ix_ai_script_runs_session_id", "ai_script_runs", ["session_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_ai_script_runs_session_id", table_name="ai_script_runs", if_exists=True)
    op.drop_index("ix_ai_script_runs_script_id", table_name="ai_script_runs", if_exists=True)
    op.drop_table("ai_script_runs", if_exists=True)

    op.drop_index("ix_ai_scripts_name", table_name="ai_scripts", if_exists=True)
    op.drop_table("ai_scripts", if_exists=True)

    # SQLite does not support DROP COLUMN; leave new columns in place on downgrade.


def _add_column_idempotent(
    table_name: str,
    col_name: str,
    col_type: sa.TypeEngine,
    server_default: str | None,
) -> None:
    """Add a column only if it doesn't already exist (dialect-independent)."""
    col_def = col_name
    if server_default is not None:
        col_def += f" {server_default}"
    op.execute(
        f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_def}"
    )
