"""Document (knowledge base) model."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256), index=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(32), index=True, default=None)
    category: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    version_tag: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[Optional[str]] = mapped_column(String(512), default=None)
    source: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    # FK to the user that uploaded the document. Nullable because legacy
    # rows predate the column. ``uploaded_by`` (string) is kept as a
    # denormalised display field for backwards-compatibility.
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Async parse status. ``pending`` = uploaded but worker hasn't parsed;
    # ``ready`` = searchable; ``failed`` = parse error (see ``index_error``).
    # The HTTP upload path schedules a Celery parse_document task that flips
    # this to ``ready`` once the body has been extracted into Whoosh.
    index_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    index_error: Mapped[Optional[str]] = mapped_column(String(500), default=None)
