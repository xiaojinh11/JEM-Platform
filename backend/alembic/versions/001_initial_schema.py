"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), unique=True, nullable=False),
        sa.Column("full_name", sa.String(128), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), default="operator"),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), unique=True, nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("secret_kind", sa.String(16), default="password"),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("enable_password_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("hostname", sa.String(128), nullable=True),
        sa.Column("vendor", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("software_version", sa.String(64), nullable=True),
        sa.Column("role", sa.String(32), default="switch"),
        sa.Column("serial_no", sa.String(64), nullable=True),
        sa.Column("location", sa.String(128), nullable=True),
        sa.Column("tags", sa.String(512), nullable=True),
        sa.Column("group_name", sa.String(64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_simulated", sa.Boolean(), default=False),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "device_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("address", sa.String(128), nullable=False),
        sa.Column("port", sa.Integer(), default=22),
        sa.Column("credential_id", sa.Integer(), sa.ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_primary", sa.Boolean(), default=True),
        sa.Column("notes", sa.String(255), nullable=True),
    )

    op.create_table(
        "config_archives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), unique=True),
        sa.Column("latest_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "config_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("archive_id", sa.Integer(), sa.ForeignKey("config_archives.id", ondelete="CASCADE")),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), default=0),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("vendor", sa.String(32), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("version_tag", sa.String(64), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), default=0),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("summary", sa.String(512), nullable=True),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), default="draft"),
        sa.Column("target_device_ids", sa.JSON(), default=list),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("initiated_by", sa.String(64), nullable=False),
        sa.Column("batch_size", sa.Integer(), default=5),
        sa.Column("celery_task_id", sa.String(128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "job_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE")),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE")),
        sa.Column("step_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), default="pending"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )

    op.create_table(
        "backup_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE")),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), default=0),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "adapter_registrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendor", sa.String(32), unique=True, nullable=False),
        sa.Column("class_path", sa.String(255), nullable=False),
        sa.Column("compatibility_level", sa.Integer(), default=0),
        sa.Column("capabilities", sa.String(512), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("verified_by", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_index("ix_devices_name", "devices", ["name"])
    op.create_index("ix_devices_vendor", "devices", ["vendor"])
    op.create_index("ix_devices_group_name", "devices", ["group_name"])
    op.create_index("ix_devices_hostname", "devices", ["hostname"])
    op.create_index("ix_devices_role", "devices", ["role"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_config_revisions_content_hash", "config_revisions", ["content_hash"])


def downgrade() -> None:
    op.drop_table("adapter_registrations")
    op.drop_table("audit_events")
    op.drop_table("backup_snapshots")
    op.drop_table("job_steps")
    op.drop_table("jobs")
    op.drop_table("documents")
    op.drop_table("config_revisions")
    op.drop_table("config_archives")
    op.drop_table("device_endpoints")
    op.drop_table("devices")
    op.drop_table("credentials")
    op.drop_table("users")
