"""add scheduled_health_task table (v1.7 scheduled / filterable health)

Revision ID: 014_add_scheduled_health_task
Revises: 013_add_health_optical_config
Create Date: 2026-10-14

Adds the ``scheduled_health_task`` table.  A row represents a
user-defined periodic health-collection plan ("snapshot these N
devices every M minutes, asking for these K metrics").

Why a dedicated table (and not, say, a JSON column on
``device_health_snapshot``):
  * The user needs to CRUD plans without re-running collection.
  * The Beat sweep needs an indexed ``(enabled, next_run_at)``
    filter — a separate table is the natural place for that
    composite index.
  * Audit: the row's ``created_by`` and ``created_at`` make it
    obvious who created which plan.

Schema is intentionally simple: we store ``device_ids`` and
``metrics`` as JSON lists.  We don't normalize into child tables
because:
  * The relationship is "plan → many devices" but the device
    side doesn't need to know which plans reference it.
  * JSON is fast to read (the sweep loads N plans per tick, not
    per device).
  * Cardinality is bounded: even a 5000-device fleet × 20 plans
    is well under 1 MB of JSON.

Index design
------------
* ``ix_scheduled_health_task_next_run_at`` — supports the sweep's
  hot query ("enabled plans whose ``next_run_at <= now``").
* ``ix_scheduled_health_task_enabled`` — supports the UI's
  "show me only enabled plans" filter.
"""
from alembic import op
import sqlalchemy as sa


revision = "014_add_scheduled_health_task"
down_revision = "013_add_health_optical_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_health_task",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("device_ids", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    # Hot-path index for the sweep: "enabled plans that are due".
    # We keep it as a single index (not a composite) so SQLite and
    # Postgres both use it without an "IS NOT NULL" predicate dance.
    op.create_index(
        "ix_scheduled_health_task_next_run_at",
        "scheduled_health_task",
        ["next_run_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_scheduled_health_task_enabled",
        "scheduled_health_task",
        ["enabled"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_health_task_enabled", table_name="scheduled_health_task", if_exists=True)
    op.drop_index("ix_scheduled_health_task_next_run_at", table_name="scheduled_health_task", if_exists=True)
    op.drop_table("scheduled_health_task", if_exists=True)
