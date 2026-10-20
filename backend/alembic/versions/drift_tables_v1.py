"""add config_drift tables + AI tables (dialect-independent, idempotent)

Revision ID: drift_tables_v1
Revises: add_job_approval
Create Date: 2026-09-19 21:37

Creates drift_baselines, config_drifts, ai_config, ai_chat_messages,
and ai_agent_logs tables using SQLAlchemy Core types.

IF NOT EXISTS guards are used so this migration is idempotent on the
stub branch (where d4201e9b8fae may reach this revision after already
having run it on the main branch).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "drift_tables_v1"
down_revision: Union[str, Sequence[str], None] = "add_job_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Drift tables ───────────────────────────────────────────────
    op.create_table(
        "drift_baselines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("baseline_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("baseline_content_hash", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["baseline_revision_id"], ["config_revisions.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index("ix_drift_baselines_device_id", "drift_baselines", ["device_id"], if_not_exists=True)

    op.create_table(
        "config_drifts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("baseline_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("current_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("lines_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lines_removed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lines_modified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_hash", sa.String(64), nullable=True),
        sa.Column("current_hash", sa.String(64), nullable=True),
        sa.Column("diff_snippet", sa.Text(), nullable=True),
        sa.Column("change_hint", sa.String(255), nullable=True),
        sa.Column("acknowledged_by", sa.String(64), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["baseline_revision_id"], ["config_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["current_revision_id"], ["config_revisions.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index("ix_config_drifts_device_id", "config_drifts", ["device_id"], if_not_exists=True)
    op.create_index("ix_config_drifts_detected_at", "config_drifts", ["detected_at"], if_not_exists=True)
    op.create_index("ix_config_drifts_status", "config_drifts", ["status"], if_not_exists=True)

    # ── AI tables ─────────────────────────────────────────────────
    op.create_table(
        "ai_config",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="openai"),
        sa.Column("base_url", sa.String(512), nullable=False, server_default="https://api.openai.com/v1"),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("model", sa.String(64), nullable=False, server_default="gpt-4o-mini"),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.3"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("active_persona", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("total_chats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_agent_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_by", sa.String(64), nullable=True),
        if_not_exists=True,
    )
    # Seed the singleton row (id=1) — idempotent
    op.execute(
        "INSERT INTO ai_config (id, provider, base_url, model, system_prompt) "
        "VALUES (1, 'openai', 'https://api.openai.com/v1', 'gpt-4o-mini', "
        "'You are the NAW (Network Automation Workbench) AI assistant.') "
        "ON CONFLICT (id) DO NOTHING"
    )

    op.create_table(
        "ai_chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(64), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    op.create_index("ix_ai_chat_messages_user_id", "ai_chat_messages", ["user_id"], if_not_exists=True)
    op.create_index("ix_ai_chat_messages_username", "ai_chat_messages", ["username"], if_not_exists=True)
    op.create_index("ix_ai_chat_messages_session_id", "ai_chat_messages", ["session_id"], if_not_exists=True)
    op.create_index("ix_ai_chat_messages_created_at", "ai_chat_messages", ["created_at"], if_not_exists=True)

    op.create_table(
        "ai_agent_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index("ix_ai_agent_logs_username", "ai_agent_logs", ["username"], if_not_exists=True)
    op.create_index("ix_ai_agent_logs_session_id", "ai_agent_logs", ["session_id"], if_not_exists=True)
    op.create_index("ix_ai_agent_logs_tool", "ai_agent_logs", ["tool"], if_not_exists=True)
    op.create_index("ix_ai_agent_logs_created_at", "ai_agent_logs", ["created_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_ai_agent_logs_created_at", table_name="ai_agent_logs", if_exists=True)
    op.drop_index("ix_ai_agent_logs_tool", table_name="ai_agent_logs", if_exists=True)
    op.drop_index("ix_ai_agent_logs_session_id", table_name="ai_agent_logs", if_exists=True)
    op.drop_index("ix_ai_agent_logs_username", table_name="ai_agent_logs", if_exists=True)
    op.drop_table("ai_agent_logs", if_exists=True)

    op.drop_index("ix_ai_chat_messages_created_at", table_name="ai_chat_messages", if_exists=True)
    op.drop_index("ix_ai_chat_messages_session_id", table_name="ai_chat_messages", if_exists=True)
    op.drop_index("ix_ai_chat_messages_username", table_name="ai_chat_messages", if_exists=True)
    op.drop_index("ix_ai_chat_messages_user_id", table_name="ai_chat_messages", if_exists=True)
    op.drop_table("ai_chat_messages", if_exists=True)

    op.drop_table("ai_config", if_exists=True)

    op.drop_index("ix_config_drifts_status", table_name="config_drifts", if_exists=True)
    op.drop_index("ix_config_drifts_detected_at", table_name="config_drifts", if_exists=True)
    op.drop_index("ix_config_drifts_device_id", table_name="config_drifts", if_exists=True)
    op.drop_table("config_drifts", if_exists=True)
    op.drop_index("ix_drift_baselines_device_id", table_name="drift_baselines", if_exists=True)
    op.drop_table("drift_baselines", if_exists=True)
