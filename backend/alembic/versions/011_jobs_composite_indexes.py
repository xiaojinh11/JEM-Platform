"""add composite indexes on jobs

Revision ID: 011_jobs_composite_indexes
Revises:
Create Date: 2026-09-25 20:30

Performance: the dashboard pending-approval counter currently runs

    WHERE requires_approval IS TRUE
      AND approved_by IS NULL
      AND status IN ('draft', 'pending')

Without a composite index, Postgres (or SQLite in dev) merges three
single-column btrees via bitmap-AND. With the composite below, it's a
direct btree seek. Status+created_at supports the common
"recently-changed-jobs" list view.

Revision ID: 011_jobs_composite_indexes
Revises: 010_add_ai_telemetry
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "011_jobs_composite_indexes"
down_revision = "010_add_ai_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_pending_approval",
        "jobs",
        ["requires_approval", "approved_by", "status"],
    )
    op.create_index(
        "ix_jobs_status_created",
        "jobs",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_index("ix_jobs_pending_approval", table_name="jobs")
