"""Registration and password-recovery business logic."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.security import hash_password, verify_password
from app.models.account import (
    LoginAttempt,
    LoginLockout,
    PasswordResetToken,
    RegistrationRequest,
)
from app.models.user import User
from app.services import account_mail
from app.services.audit import record as audit_record

# ── Configuration ─────────────────────────────────────────────────────────────
VERIFICATION_CODE_TTL = timedelta(minutes=15)
RESET_TOKEN_TTL = timedelta(minutes=30)
MAX_VERIFICATION_ATTEMPTS = 5
LOCKOUT_THRESHOLD = 5           # failed attempts before lockout
LOCKOUT_WINDOW = timedelta(minutes=15)

# Conservative email regex — covers the common cases, defers strict
# validation to the verification step. We accept RFC-style addresses but
# reject obvious typos (no @, spaces, multiple @, missing TLD).
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)
_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{1,63}$")


# ── Validation helpers ───────────────────────────────────────────────────────
def is_valid_email(value: str) -> bool:
    return bool(value) and len(value) <= 255 and bool(_EMAIL_RE.match(value))


def is_valid_username(value: str) -> bool:
    return bool(value) and bool(_USERNAME_RE.match(value))


def is_strong_password(value: str) -> bool:
    """Require ≥8 chars, mixed case + digit. Permissive but blocks the worst."""
    if not value or len(value) < 8 or len(value) > 128:
        return False
    has_lower = any(c.islower() for c in value)
    has_upper = any(c.isupper() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return has_lower and has_upper and has_digit


# ── Registration ─────────────────────────────────────────────────────────────
def request_registration(db: Session, email: str) -> tuple[bool, str, bool]:
    """Send a verification code to *email*. Idempotent on rate-limit.

    Returns ``(ok, message, smtp_sent)``. We always return success even if the email
    is already taken or rate-limited to avoid leaking which emails are
    registered (the only signal is whether the verification succeeds).
    ``smtp_sent`` is False when SMTP is not configured and the code was
    only logged to console (development mode).
    """
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return False, "邮箱格式不正确", False

    # Already a registered user? Send a "pending review" mail instead so
    # we don't silently drop them.
    existing_user = db.query(User).filter_by(email=email).first()
    if existing_user:
        account_mail.send_registration_pending_notice(email, existing_user.username)
        return True, "验证码已发送", False

    # Invalidate any stale pending request for this email
    db.query(RegistrationRequest).filter_by(email=email, status="pending").delete()

    code = account_mail.generate_registration_code()
    req = RegistrationRequest(
        email=email,
        verification_code=code,
        code_expires_at=datetime.now(timezone.utc) + VERIFICATION_CODE_TTL,
    )
    db.add(req)
    db.commit()

    ok, _msg, smtp_sent = account_mail.send_verification_code(email, code)
    if not ok:
        # Roll back the pending row so the user can retry without a stale code.
        db.delete(req)
        db.commit()
        return False, "邮件发送失败，请稍后重试", False
    if smtp_sent:
        return True, "验证码已发送，请查收邮件", True
    return True, "验证码已发送，请在后端控制台查看", False


def verify_and_register(
    db: Session,
    email: str,
    code: str,
    username: str,
    password: str,
    full_name: Optional[str] = None,
    request_ip: Optional[str] = None,
) -> tuple[bool, str, Optional[User]]:
    """Verify the code and create the user.

    Returns ``(ok, message, user)``. On failure the user is ``None``.
    """
    email = (email or "").strip().lower()
    username = (username or "").strip()

    if not is_valid_email(email):
        return False, "邮箱格式不正确", None
    if not is_valid_username(username):
        return False, "用户名必须以字母开头，长度 2-64，仅含字母数字下划线连字符", None
    if not is_strong_password(password):
        return False, "密码至少 8 位，且包含大小写字母与数字", None

    req = (
        db.query(RegistrationRequest)
        .filter_by(email=email, status="pending")
        .first()
    )
    if not req:
        return False, "验证码无效或已过期，请重新获取", None
    if datetime.now(timezone.utc) > _as_utc(req.code_expires_at):
        req.status = "expired"
        db.commit()
        return False, "验证码已过期，请重新获取", None
    if req.attempts >= MAX_VERIFICATION_ATTEMPTS:
        req.status = "expired"
        db.commit()
        return False, "验证码尝试次数过多，请重新获取", None
    if not code or code.strip() != req.verification_code:
        req.attempts += 1
        db.commit()
        return False, "验证码错误", None

    if db.query(User).filter_by(username=username).first():
        return False, "用户名已被占用", None
    if db.query(User).filter_by(email=email).first():
        return False, "邮箱已注册", None

    user = User(
        username=username,
        email=email,
        full_name=full_name or None,
        password_hash=hash_password(password),
        # Operators get a real account but **admin must enable it** before
        # they can log in. This prevents random strangers from registering
        # and immediately poking around.
        role="operator",
        is_active=False,
    )
    db.add(user)
    db.flush()

    req.status = "approved"
    req.consumed_at = datetime.now(timezone.utc)

    audit_record(
        db,
        actor=username,
        action="auth.register",
        target_kind="user",
        target_id=str(user.id),
        detail=f"email={email} (pending admin approval)",
        ip_address=request_ip,
    )
    db.commit()
    db.refresh(user)

    # Tell the user their account is awaiting admin review.
    try:
        account_mail.send_registration_pending_notice(email, username)
    except Exception:  # noqa: BLE001
        logger.warning("failed to send post-registration notice to {}", email)

    return True, "注册成功，请等待管理员审核", user


# ── Password reset ───────────────────────────────────────────────────────────
def request_password_reset(db: Session, email: str, request_ip: Optional[str] = None) -> tuple[bool, str]:
    """Email a reset link to *email* if the user exists.

    We always return success regardless of whether the email is registered
    — leaking that information would let attackers enumerate accounts.
    """
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return False, "邮箱格式不正确"

    user = db.query(User).filter_by(email=email).first()
    if user and user.is_active:
        raw_token = account_mail.generate_reset_token()
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=datetime.now(timezone.utc) + RESET_TOKEN_TTL,
            )
        )
        db.commit()

        reset_url = f"{settings.public_base_url}/#/reset-password?token={raw_token}"
        ok, _msg = account_mail.send_password_reset_link(email, reset_url)
        if not ok:
            return False, "邮件发送失败，请稍后重试"
        audit_record(
            db,
            actor=user.username,
            action="auth.password_reset_request",
            target_kind="user",
            target_id=str(user.id),
            ip_address=request_ip,
        )
        db.commit()

    return True, "如果该邮箱已注册，重置链接已发送"


def confirm_password_reset(
    db: Session, token: str, new_password: str, request_ip: Optional[str] = None
) -> tuple[bool, str]:
    """Validate the reset token and apply the new password."""
    if not is_strong_password(new_password):
        return False, "密码至少 8 位，且包含大小写字母与数字"
    if not token:
        return False, "无效的重置链接"

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.query(PasswordResetToken).filter_by(token_hash=token_hash).first()
    if not row or row.used_at is not None or datetime.now(timezone.utc) > _as_utc(row.expires_at):
        return False, "重置链接无效或已过期"

    user = db.get(User, row.user_id)
    if not user:
        return False, "账户不存在"

    # Refuse to "reset" to the same password — defeats the whole point.
    if verify_password(new_password, user.password_hash):
        return False, "新密码不能与当前密码相同"

    user.password_hash = hash_password(new_password)
    row.used_at = datetime.now(timezone.utc)
    # Reset any active lockout so the user can immediately log in.
    lockout = db.query(LoginLockout).filter_by(username=user.username).first()
    if lockout:
        db.delete(lockout)

    audit_record(
        db,
        actor=user.username,
        action="auth.password_reset_complete",
        target_kind="user",
        target_id=str(user.id),
        ip_address=request_ip,
    )
    db.commit()
    return True, "密码已重置，请使用新密码登录"


# ── Login lockout ────────────────────────────────────────────────────────────
def record_login_attempt(
    db: Session, username: str, ip: Optional[str], success: bool
) -> None:
    """Persist the attempt and bump the lockout counter on failure."""
    db.add(LoginAttempt(username=username, ip_address=ip, success=success))
    if success:
        lockout = db.query(LoginLockout).filter_by(username=username).first()
        if lockout:
            db.delete(lockout)
    else:
        lockout = db.query(LoginLockout).filter_by(username=username).first()
        recent_failures = (
            db.query(LoginAttempt)
            .filter(LoginAttempt.username == username, LoginAttempt.success.is_(False))
            .filter(LoginAttempt.created_at >= datetime.now(timezone.utc) - LOCKOUT_WINDOW)
            .count()
        )
        if lockout is None:
            lockout = LoginLockout(username=username, failed_count=1, locked_until=datetime.now(timezone.utc))
            db.add(lockout)
        else:
            lockout.failed_count = recent_failures
        if recent_failures >= LOCKOUT_THRESHOLD:
            lockout.locked_until = datetime.now(timezone.utc) + LOCKOUT_WINDOW
    db.commit()


def is_locked_out(db: Session, username: str) -> bool:
    lockout = db.query(LoginLockout).filter_by(username=username).first()
    if not lockout:
        return False
    if datetime.now(timezone.utc) > _as_utc(lockout.locked_until):
        # Stale lockout — clear it.
        db.delete(lockout)
        db.commit()
        return False
    return True


# ── Helpers ──────────────────────────────────────────────────────────────────
def _as_utc(dt: datetime) -> datetime:
    """Normalise a possibly-naive datetime to a UTC-aware one."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
