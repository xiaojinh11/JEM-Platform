"""AI-generated operational alerts.

When the drift scheduler (or any other background job) needs a human-readable
explanation of an event, it asks the AI to write one and persists the result
here. The frontend polls /api/v1/ai/alerts for the latest ones, and the
WebSocket broadcaster pushes new entries live.

Each row is the AI's analysis of one underlying event (drift / login failure
/ etc.). Storing the analysis as a plain row keeps the hot path synchronous —
the AI call runs in a background thread/queue and just fills this table.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _utcnow() -> callable:
    return lambda: datetime.utcnow()


class AIAlert(Base):
    """One AI-generated alert. The frontend reads these to render the
    monitor persona's '主动告警' panel and to show a toast badge.

    The combination of (source_kind, source_id) is unique so we don't store
    two analyses of the same drift. When a re-analysis is desired
    (e.g. user explicitly asked "再分析一次"), the caller should DELETE the
    old row before INSERT — keeping that semantics simple beats trying to
    model a history.

    `status` is one of: pending (analysis queued), ready (text written),
    failed (AI returned error), superseded (a newer analysis replaced this).
    """

    __tablename__ = "ai_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What triggered the alert: drift | login | device_offline | custom
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    # Foreign id in the source table (drift_id for drift, etc.)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    # Severity inherited from the source — never computed by the AI.
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    # Headline shown in the UI badge + email subject.
    title: Mapped[str] = mapped_column(String(255), default="")
    # The full AI analysis as Markdown. May be empty when status='pending'
    # or 'failed'.
    body: Mapped[str] = mapped_column(Text, default="")
    # Free-form metadata for the UI: {"device_id": 5, "drift_id": 12, ...}.
    extra: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    # Lifecycle of this analysis. See class docstring.
    status: Mapped[str] = mapped_column(String(16), default="ready", index=True)
    # AI token / round stats (for diagnostics).
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # Optional link back to the AIChatMessage that produced the analysis,
    # so power users can "show me how the AI thought about this".
    chat_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ai_chat_messages.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow(), onupdate=_utcnow
    )
