"""add optical / config_fingerprint / metrics_run to device_health_snapshot

Revision ID: 013_add_health_optical_config
Revises: 012_add_device_health_snapshot
Create Date: 2026-10-14

This migration is the v1.7 "scheduled / filterable health collection"
feature. We add five new columns to ``device_health_snapshot``:

* ``optical`` (JSON) — per-port RX/TX dBm readings.  Default ``NULL``
  for rows written before the column existed; new rows default to ``[]``
  via the application layer.
* ``config_fingerprint`` (String(64)) — sha256 of the captured
  config excerpt, 64 hex chars.  Empty string is the application-
  level "not collected" sentinel.
* ``config_lines`` (Integer, nullable) — line count of the excerpt.
  We do NOT enforce non-negative here; the app layer clamps to 0+
  before writing.
* ``config_excerpt`` (Text) — verbatim first ~100 lines of running
  config, capped at 2KB.  Empty string when not collected.
* ``metrics_run`` (JSON) — list of metric names actually executed
  for this row.  Lets the UI distinguish "this column is zero
  because the device says 0" from "this column is empty because
  the schedule didn't ask for it".

Why all nullable / default-null:
  Existing rows (pre-v1.7) had no notion of optical or config
  excerpt; making the columns NOT NULL would require a backfill.
  Instead the application writes defaults (``[]``, ``""``, ``0``,
  ``NULL``) for new rows, and the UI treats NULL as "feature not
  available" — i.e. a clear, non-falsifiable signal that the row
  predates this migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "013_add_health_optical_config"
down_revision = "012_add_device_health_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("device_health_snapshot") as batch_op:
        batch_op.add_column(
            sa.Column("optical", sa.JSON(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("config_fingerprint", sa.String(64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("config_lines", sa.Integer(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("config_excerpt", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("metrics_run", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("device_health_snapshot") as batch_op:
        batch_op.drop_column("metrics_run")
        batch_op.drop_column("config_excerpt")
        batch_op.drop_column("config_lines")
        batch_op.drop_column("config_fingerprint")
        batch_op.drop_column("optical")
