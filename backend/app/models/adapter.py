"""Adapter registration: persists supported vendors + compatibility level."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AdapterRegistration(Base):
    __tablename__ = "adapter_registrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    class_path: Mapped[str] = mapped_column(String(255))
    compatibility_level: Mapped[int] = mapped_column(Integer, default=0)
    capabilities: Mapped[str] = mapped_column(String(512))  # comma-separated
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    verified_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
