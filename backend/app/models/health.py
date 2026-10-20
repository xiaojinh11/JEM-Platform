"""Per-device health snapshot model.

Backed by the ``device_health_snapshot`` table (see Alembic migration
``012_add_device_health_snapshot``). Stores one row per
``collect_health`` call — a small, time-series-friendly table that the
UI dashboard will query for the "last seen" status of every device.

We keep the schema **flat** (no separate devices/sensors table) for two
reasons:

1. Health is a side-channel — we don't model the network topology here.
2. The table is meant to be pruned/partitioned by ``collected_at``;
   normalization would only complicate the lifecycle.

If the project later needs per-sensor resolution (e.g. "fan 2 of 4
is abnormal"), add a sibling table rather than retrofitting this one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class DeviceHealthSnapshot(Base):
    """A single health reading for a device at a point in time."""

    __tablename__ = "device_health_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True,
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow(), index=True,
    )
    # Vendor at the time of collection — captured so historical rows
    # remain meaningful even if a device is later re-tagged to a
    # different vendor.
    vendor: Mapped[str] = mapped_column(String(32), default="")

    # All numeric metrics are nullable: a single bad command should
    # not poison the row.  Values are percentages (0–100) for cpu/mem
    # and absolute (Celsius / Watts) for temp / power.
    cpu_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), default=None)
    mem_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), default=None)
    temp_c: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), default=None)
    # Coarse status string — see health_collector._parse_fan for the
    # exact vocab.  We keep it as a free-form String to avoid an
    # Alembic enum migration when the vocabulary grows.
    fan_state: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    power_w: Mapped[Optional[float]] = mapped_column(Numeric(7, 2), default=None)

    # "collect" | "manual" | "backfill" — distinguishes the new Celery
    # task from any ad-hoc collections we add later.
    source: Mapped[str] = mapped_column(String(32), default="collect")

    # First 1KB of raw device output.  Useful for debugging "why is
    # this row weird" without re-running the collection.
    raw_excerpt: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Per-metric error messages (e.g. "fan: command not supported").
    # JSON list of short strings; intentionally not normalized.
    errors: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # ── v1.7: scheduled / filterable health collection ──────────────
    # Per-port optical (RX/TX dBm) readings. Empty list (not NULL) if
    # the device doesn't support DDM or the schedule didn't ask for
    # this metric. Each entry is ``{"port": "...", "rx_dbm": -7.23,
    # "tx_dbm": -5.10}``; missing values are None.
    optical: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # sha256 of the captured config excerpt. Empty string if the
    # schedule didn't ask for the ``config`` metric or the device
    # didn't return anything parseable. Pair with ``config_lines`` /
    # ``config_excerpt`` for "did the config change since last run?"
    # comparisons in the UI.
    config_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    # Line count of the captured excerpt (capped at the collector's
    # head-100 limit; informational only, not the full running-config
    # length).
    config_lines: Mapped[Optional[int]] = mapped_column(default=None)
    # Verbatim first ~100 lines of running config, capped at 2KB.
    # Intentionally lossy; full config is in the configs/revisions
    # table. Empty string if not collected this run.
    config_excerpt: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # Metric names actually executed for this row. Tells the UI which
    # columns are "not collected yet" vs. "collected and zero" (a real
    # difference: zero is a valid reading; not-collected is a known
    # gap because the schedule didn't ask).
    metrics_run: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # Convenience relationship — the device row isn't loaded by
    # default, but a UI listing can join via ``selectinload``.
    device = relationship("Device")
