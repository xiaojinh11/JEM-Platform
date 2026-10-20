"""Credential vault model. Plaintext secrets are never persisted."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Credential(Base):
    """Encrypted credential entry (username + password or username + private key).

    `secret_enc` stores Fernet-encrypted payload (password or private key).

    `device_id` is optional — credentials can be shared across a fleet
    (e.g. a service account used by every device in a vendor class).
    When set, the graph visualiser draws a ``CRED_OF_DEVICE`` edge.
    """

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    username: Mapped[str] = mapped_column(String(64))
    secret_kind: Mapped[str] = mapped_column(String(16), default="password")  # password | ssh_key
    secret_enc: Mapped[str] = mapped_column(Text)
    enable_password_enc: Mapped[Optional[str]] = mapped_column(Text, default=None)
    device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
