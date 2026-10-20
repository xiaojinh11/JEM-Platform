"""Config drift detection models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class DriftSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class DriftStatus(str, Enum):
    NEW = "new"          # 未读
    ACKNOWLEDGED = "acknowledged"


class DriftBaseline(Base):
    """User-selected gold baseline revision for a device.

    When set, the drift scanner compares live config against this snapshot
    instead of the most-recent backup.
    """

    __tablename__ = "drift_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), unique=True, index=True
    )
    baseline_revision_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("config_revisions.id", ondelete="SET NULL"), default=None
    )
    # If no revision is set, we use the live config we fetched at baseline time.
    baseline_content_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    device: Mapped["Device"] = relationship(back_populates="drift_baseline")


class ConfigDrift(Base):
    """A single drift event for a device at a point in time."""

    __tablename__ = "config_drifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    # The revision that was used as baseline (may be None → live baseline)
    baseline_revision_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("config_revisions.id", ondelete="SET NULL"), default=None
    )
    # Revision that was current when drift was detected
    current_revision_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("config_revisions.id", ondelete="SET NULL"), default=None
    )

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    severity: Mapped[str] = mapped_column(
        SAEnum(DriftSeverity, values_callable=lambda x: [e.value for e in x]),
        default=DriftSeverity.WARNING.value,
    )
    status: Mapped[str] = mapped_column(
        SAEnum(DriftStatus, values_callable=lambda x: [e.value for e in x]),
        default=DriftStatus.NEW.value,
        index=True,
    )

    # Numeric summary
    lines_added: Mapped[int] = mapped_column(Integer, default=0)
    lines_removed: Mapped[int] = mapped_column(Integer, default=0)
    lines_modified: Mapped[int] = mapped_column(Integer, default=0)

    # Stored hashes for quick re-detection
    baseline_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    current_hash: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    # Plain-text diff snippet (first ~2 KB of unified diff)
    diff_snippet: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Who made the change (heuristic, best-effort)
    change_hint: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)

    device: Mapped["Device"] = relationship(back_populates="drifts")
