"""Add email to users + registration / password-reset tables

Revision ID: 003_add_account_tables
Revises: 006_merge
Create Date: 2026-09-20

Linear chain after merge:
  006_merge -> 003 -> 004 -> 005 (head)

All prior schema is created by the upstream chain; this migration only
adds the account/email tables.

Adds:
- ``users.email`` (unique, nullable)
- ``registration_requests`` table for the email-code sign-up flow
- ``password_reset_tokens`` table for the email-link reset flow
- ``login_attempts`` + ``login_lockouts`` tables for brute-force defence
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_add_account_tables"
down_revision: Union[str, Sequence[str], None] = "ai_scripts_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users.email — nullable so existing rows (with no email) still validate.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("email", sa.String(length=255), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # 2. registration_requests
    op.create_table(
        "registration_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("verification_code", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("code_expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_registration_requests_email", "registration_requests", ["email"], unique=True)
    op.create_index("ix_registration_requests_code", "registration_requests", ["verification_code"])
    op.create_index("ix_registration_requests_status", "registration_requests", ["status"])

    # 3. password_reset_tokens
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True)

    # 4. login_attempts
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_attempts_username", "login_attempts", ["username"])
    op.create_index("ix_login_attempts_created_at", "login_attempts", ["created_at"])

    # 5. login_lockouts
    op.create_table(
        "login_lockouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_login_lockouts_username", "login_lockouts", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_login_lockouts_username", table_name="login_lockouts")
    op.drop_table("login_lockouts")
    op.drop_index("ix_login_attempts_created_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_username", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_registration_requests_status", table_name="registration_requests")
    op.drop_index("ix_registration_requests_code", table_name="registration_requests")
    op.drop_index("ix_registration_requests_email", table_name="registration_requests")
    op.drop_table("registration_requests")
    op.drop_index("ix_users_email", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email")
