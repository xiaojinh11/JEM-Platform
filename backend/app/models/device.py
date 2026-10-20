"""Device and endpoint models."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(128), index=True, default=None)
    vendor: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    software_version: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    role: Mapped[str] = mapped_column(String(32), default="switch", index=True)
    serial_no: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    location: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    tags: Mapped[Optional[str]] = mapped_column(String(512), default=None)  # comma-separated
    group_name: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[Optional[dict]] = mapped_column("metadata_json", JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    endpoints: Mapped[list["DeviceEndpoint"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    drifts: Mapped[list["ConfigDrift"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    drift_baseline: Mapped[Optional["DriftBaseline"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )


class DeviceEndpoint(Base):
    __tablename__ = "device_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # inband_ssh | mgmt_ssh | console | restconf | netconf
    address: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(Integer, default=22)
    credential_id: Mapped[Optional[int]] = mapped_column(ForeignKey("credentials.id", ondelete="SET NULL"), default=None)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    device: Mapped["Device"] = relationship(back_populates="endpoints")
