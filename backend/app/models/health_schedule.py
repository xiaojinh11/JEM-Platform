"""Scheduled health-collection task.

Backed by the ``scheduled_health_task`` table (see Alembic migration
``014_add_scheduled_health_task``).

Why a dedicated table
---------------------
We already have Celery Beat — the natural place for "every N minutes,
run X" lives in ``celery_app.conf.beat_schedule``.  But hard-coding
"every 5 minutes" in the app config doesn't let an operator say "I
want a 2-minute scan of just the access-layer switches".  This table
captures the *user-defined* schedules: a name, an interval, a
device list, a metric filter, and an enabled flag.  Celery Beat fires
the lightweight ``naw.health.scheduled_sweep`` task once a minute;
that task queries the table for due rows and enqueues one
``naw.health.snapshot_one`` per device.

A row that is "due" satisfies::

    enabled IS TRUE AND (last_run_at IS NULL OR
        last_run_at + interval_seconds <= now())

Idempotence
-----------
The sweep claims each row by setting ``last_run_at = now()`` *before*
dispatching the per-device tasks.  If the sweep crashes between
"claim" and "dispatch", the worst that happens is the next sweep sees
the row as recently-run and skips it.  That's fine: the per-device
snapshot is itself idempotent (just writes a new row), so we never
lose data — we just delay a cycle by a minute.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ScheduledHealthTask(Base):
    """A user-defined recurring health collection.

    * ``device_ids`` — JSON list of integers (empty = every device).
      We use JSON rather than a join table because the cardinality
      is bounded (one schedule, ~hundreds of devices) and a join
      table would force the API to maintain an extra resource for
      no real benefit.
    * ``metrics`` — JSON list of metric names (empty = all).  Mirrors
      the ``metrics`` arg of ``collect_health``.  Recognised values
      are ``cpu``, ``mem``, ``temp``, ``fan``, ``power``,
      ``optical``, ``config``.
    * ``interval_seconds`` — between 60 (1 min) and 86400 (1 day).
      We refuse anything tighter than 60s to keep Beat's per-minute
      sweep from stampeding the same schedule.
    * ``enabled`` — soft kill-switch.  Disabling a row stops new
      runs but doesn't delete the row.
    * ``last_run_at`` — when the sweep most-recently *claimed* this
      row (NOT when the last per-device snapshot completed).  Used
      for "is it due?" math.
    * ``next_run_at`` — denormalised for the UI; the sweep keeps it
      in sync.
    """

    __tablename__ = "scheduled_health_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    device_ids: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    metrics: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow(), onupdate=lambda: datetime.utcnow(),
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None,
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None,
    )
    last_status: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    last_summary: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    # Audit: who created it. Nullable so backfills / migrations can
    # write a row without a user context.
    created_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    # Convenience relationships (not loaded by default).
    # device_ids is a JSON list so we don't have a Device join here
    # — the worker resolves device_ids -> Device rows at dispatch time.
