"""Scheduled health collection task.

Backed by the ``scheduled_health_task`` table (see Alembic migration
``014_add_scheduled_health_task``).  One row per "I want the system
to snapshot these devices every N minutes" plan.  The user-facing
CRUD lives at ``/api/v1/health/schedules``.

Design choices (and why they aren't freestanding in celery_beat)::

* **No Celery Beat schedule entries** — we explicitly do NOT
  register every plan as a Beat task.  Why?  Beat entries are
  static (loaded at process start); users add/remove plans at
  runtime.  Instead, the **single** ``naw.health.scheduled_sweep``
  task fires every 60s, looks at every enabled plan whose
  ``next_run_at`` is in the past, and dispatches one
  ``naw.health.snapshot_one`` per device.  This trades a tiny
  per-tick DB read for the ability to CRUD plans without
  restarting Beat.

* **Per-plan metric filter** — ``metrics`` is a JSON list of
  metric names (``cpu``, ``mem``, ``temp``, ``fan``, ``power``,
  ``optical``, ``config``).  An empty list means "all metrics"
  — keeps the contract identical to the manual endpoint.

* **Per-plan device filter** — ``device_ids`` is a JSON list of
  device ids.  Empty list means "every device in the inventory"
  (same contract as the manual endpoint).  Operators typically
  scope to a small group ("core switches only") for both
  performance and signal-to-noise reasons.

* **Interval semantics** — ``interval_minutes`` is a *minimum*
  spacing, not a fixed schedule.  A sweep that takes longer
  than the interval will push ``next_run_at`` to (last_finish
  + interval) so we never have two parallel runs of the same
  plan.

* **Disabled = keep row, stop firing** — we never delete a plan
  just because it's been disabled.  Audit trail + the ability
  to re-enable later.

* **No approval gate** — health snapshots are read-only, mirror
  the manual endpoint's contract (``require_operator``, not
  ``require_admin``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ScheduledHealthTask(Base):
    """A user-defined periodic health-collection plan."""

    __tablename__ = "scheduled_health_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Display name shown in the UI.  Free-form; not unique so two
    # operators can independently name their plan "core-switches".
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # "0" minutes is rejected at the API layer; this is a safety
    # belt for any direct DB writes.  We use Integer to keep
    # arithmetic simple (no timezone games).
    interval_minutes: Mapped[int] = mapped_column(default=60)
    # JSON list of device ids.  Empty list => every device in
    # the inventory (sweep-time resolution).  We don't FK to
    # ``devices`` because we want plans to survive device
    # deletion (the sweep just skips missing ids).
    device_ids: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # JSON list of metric names.  Empty list => all metrics.
    metrics: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Master switch.  Disabled plans are kept (audit trail) but
    # never dispatched by the sweep.
    enabled: Mapped[bool] = mapped_column(default=True)
    # Last time the sweep dispatched a snapshot for this plan.
    # Null = never fired yet.  Used for the "X minutes ago" UI
    # badge and for backoff (next_run_at = last_fired +
    # interval_minutes).
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None,
    )
    # When the sweep should next consider this plan.  The sweep
    # dispatches only if ``now >= next_run_at``.  We write back
    # after every dispatch so a half-failed plan doesn't
    # immediately re-fire on the next tick.
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True,
    )
    # Free-form notes.  We don't normalize; a plan's reason for
    # existing lives here.
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # Audit: who created it, when.  Username (string) rather
    # than FK so plan rows survive user deletion.
    created_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.utcnow(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None,
    )

    # Convenience: not used by the application but kept around
    # for symmetry with other "row" models.  Doesn't trigger a
    # query unless explicitly accessed.
    creator = relationship(
        "User", primaryjoin="foreign(ScheduledHealthTask.created_by)==User.username",
        viewonly=True,
    )
