"""Models for user registration verification, password reset, and login lockout."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RegistrationRequest(Base):
    """Pending user registration. Created when a user requests access.

    The 6-digit code is emailed to them; they must enter it + choose a
    username/password within ``code_expires_at`` to activate the account.

    Once consumed, ``status`` flips to ``approved`` and a ``User`` row is
    created; admin can later promote the role.
    """

    __tablename__ = "registration_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    verification_code: Mapped[str] = mapped_column(String(16), index=True)
    # ``pending`` until code entered & password set;
    # ``approved`` once User row is created (requires admin to enable login);
    # ``rejected`` / ``expired`` for housekeeping.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    code_expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


class PasswordResetToken(Base):
    """Single-use token for resetting a password via email link.

    Tokens are 43 random URL-safe characters, hashed in storage (the
    plaintext only lives in the email body).
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class LoginAttempt(Base):
    """Track failed login attempts for rate-limiting / lockout."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class LoginLockout(Base):
    """Per-username lockout window. Cleared on successful login."""

    __tablename__ = "login_lockouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    locked_until: Mapped[datetime] = mapped_column(DateTime)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
