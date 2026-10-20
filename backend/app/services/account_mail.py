"""Account-related email delivery (registration codes, password reset links).

Centralised here so the auth endpoints don't have to know SMTP details.
"""
from __future__ import annotations

import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.core.config import settings
from app.core.logging import logger


def _smtp_send(to_email: str, subject: str, body_text: str, body_html: Optional[str] = None) -> tuple[bool, str]:
    """Send a single email through the configured SMTP server.

    Returns ``(ok, message)`` — callers log and surface a generic error to
    the client so we don't leak provider-specific failure detail.
    """
    if not settings.smtp_host:
        # No SMTP configured — fall back to console logging. Useful for
        # offline development and unit tests; never enable in production.
        logger.warning(
            "[DEV MAIL] to={} subject={!r} body={}",
            to_email, subject, body_text,
        )
        return True, "logged-to-console"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or "noreply@localhost"
        msg["To"] = to_email

        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port or 25, timeout=15) as srv:
            srv.ehlo()
            if settings.smtp_user and settings.smtp_password:
                srv.starttls()
                srv.ehlo()
                srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)
        logger.info("account email sent: to={} subject={}", to_email, subject)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        logger.warning("account email send failed to={}: {}", to_email, exc)
        return False, str(exc)


def send_verification_code(to_email: str, code: str) -> tuple[bool, str, bool]:
    """Email the 6-digit registration / login verification code.

    Returns ``(ok, message, smtp_sent)`` — smtp_sent=False means the code
    was only logged to console (no SMTP configured in development).
    """
    subject = "[JEM] 您的注册验证码"
    body_text = (
        f"您好，\n\n"
        f"您的注册验证码是：{code}\n\n"
        f"验证码有效期为 15 分钟，请尽快完成注册。\n"
        f"如果这不是您本人操作，请忽略本邮件。\n\n"
        f"— JEM Platform"
    )
    ok, msg = _smtp_send(to_email, subject, body_text)
    smtp_sent = settings.smtp_host is not None
    return ok, msg, smtp_sent


def send_password_reset_link(to_email: str, reset_url: str) -> tuple[bool, str]:
    """Email the password reset link.

    ``reset_url`` is the full clickable URL the user opens in their browser.
    """
    subject = "[JEM] 密码重置链接"
    body_text = (
        f"您好，\n\n"
        f"我们收到了您的密码重置请求。点击下方链接重置密码：\n\n"
        f"{reset_url}\n\n"
        f"链接有效期为 30 分钟，每个链接只能使用一次。\n"
        f"如果这不是您本人操作，请忽略本邮件，您的密码不会被更改。\n\n"
        f"— JEM Platform"
    )
    return _smtp_send(to_email, subject, body_text)


def send_registration_pending_notice(to_email: str, username: str) -> tuple[bool, str]:
    """Tell the user their account was created but needs admin approval."""
    subject = "[JEM] 注册申请已收到，等待管理员审核"
    body_text = (
        f"您好 {username}，\n\n"
        f"您的注册申请已提交。管理员审核通过后您将可以登录。\n"
        f"如有疑问请联系系统管理员。\n\n"
        f"— JEM Platform"
    )
    return _smtp_send(to_email, subject, body_text)


def generate_registration_code() -> str:
    """Cryptographically random 6-digit code (zero-padded)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_reset_token() -> str:
    """Cryptographically random URL-safe token (43 chars from 32 bytes)."""
    return secrets.token_urlsafe(32)
