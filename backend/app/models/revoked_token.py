"""Revoked JWT registry.

When a user hits ``POST /api/v1/auth/logout`` (or an admin forces
revocation) we store the *hash* of the JWT here.  ``decode_token`` and
``get_current_user`` consult this table on every authenticated request
so a leaked or stolen token can no longer be replayed after the user
logs out.

We persist a SHA-256 hash of the token instead of the token itself —
this matches the password-reset-token pattern and means a database
leak doesn't immediately expose live bearer tokens.

``expires_at`` mirrors the JWT's own ``exp`` claim.  Rows past that
time are harmless but can be pruned by a background job to keep the
table small.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # SHA-256 hex of the raw JWT.  Indexed — every authenticated request
    # does ``WHERE token_hash = ?`` against this column.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, default=None)
    username: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    # When the JWT itself expires — used for safe pruning.
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[Optional[str]] = mapped_column(String(64), default=None)
