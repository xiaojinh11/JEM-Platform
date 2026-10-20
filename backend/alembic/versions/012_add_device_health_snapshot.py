"""add device_health_snapshot table (concurrent health collection)

Revision ID: 012_add_device_health_snapshot
Revises: 011_jobs_composite_indexes
Create Date: 2026-10-14

Stores per-device health snapshots produced by
``app.services.health_collector``. One row per device per snapshot —
designed to be a time-series source for the drift/health UI.

Notes on the schema:

* All numeric metrics are nullable — a single bad command output
  (e.g. "fan command not supported on this firmware") shouldn't
  fail the whole snapshot row.
* ``raw_excerpt`` is a 1KB peek of the raw command output so an
  operator can debug a "why is fan_state=unknown" case without
  having to re-run the collection.
* ``source`` lets us tell apart the new Celery task from ad-hoc
  collections or backfills.
* ``ON DELETE CASCADE`` on ``device_id`` mirrors the drift tables'
  contract — when a device is removed, its health history goes too.
"""
from alembic import op
import sqlalchemy as sa


revision = "012_add_device_health_snapshot"
down_revision = "011_jobs_composite_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_health_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("vendor", sa.String(32), nullable=False, server_default=""),
        sa.Column("cpu_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("mem_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("temp_c", sa.Numeric(5, 2), nullable=True),
        sa.Column("fan_state", sa.String(16), nullable=True),
        sa.Column("power_w", sa.Numeric(7, 2), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="collect"),
        sa.Column("raw_excerpt", sa.Text(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    # Index that supports the common query: "give me device X's most
    # recent 100 snapshots".  Composite ordering matches the WHERE.
    op.create_index(
        "ix_device_health_snapshot_device_collected",
        "device_health_snapshot",
        ["device_id", "collected_at"],
        if_not_exists=True,
    )
    # "Give me the latest snapshot for every device" — Postgres can
    # do a loose-index scan with this; SQLite picks a recent subset.
    op.create_index(
        "ix_device_health_snapshot_collected_at",
        "device_health_snapshot",
        ["collected_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_device_health_snapshot_collected_at", table_name="device_health_snapshot", if_exists=True)
    op.drop_index("ix_device_health_snapshot_device_collected", table_name="device_health_snapshot", if_exists=True)
    op.drop_table("device_health_snapshot", if_exists=True)
