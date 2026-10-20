"""Unified notification service for all channels.

Supports:
- WebSocket broadcast (in-process, for the live bell icon in the UI)
- Email (SMTP)
- WeChat / DingTalk webhook (HTTP POST to a configured URL)
"""
from __future__ import annotations

import smtplib
import json
import textwrap
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.models.device import Device


class NotificationPayload:
    """Plain-data notification object (channel-agnostic)."""

    def __init__(
        self,
        title: str,
        body: str,
        level: str = "info",   # info | warning | critical
        device_id: Optional[int] = None,
        device_name: Optional[str] = None,
        extra: Optional[dict] = None,
    ):
        self.title = title
        self.body = body
        self.level = level
        self.device_id = device_id
        self.device_name = device_name
        self.extra = extra or {}

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "extra": self.extra,
        }


# ── In-memory WebSocket registry ─────────────────────────────────────────────
# Each connection registers itself here.  Real deployments replace with Redis.
_websocket_clients: list = []


def register_ws(client) -> None:
    _websocket_clients.append(client)


def unregister_ws(client) -> None:
    if client in _websocket_clients:
        _websocket_clients.remove(client)


def _broadcast_ws(payload: NotificationPayload) -> None:
    import asyncio
    data = json.dumps(payload.to_dict())
    # We cannot await from a sync context; schedule it.
    for client in list(_websocket_clients):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(client.send(data))
            else:
                loop.run_until_complete(client.send(data))
        except Exception:
            unregister_ws(client)


def _send_email(payload: NotificationPayload) -> None:
    if not settings.smtp_host:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[{payload.level.upper()}] {payload.title}"
        msg["From"] = settings.smtp_from or "noreply@localhost"
        msg["To"] = settings.smtp_to or ""

        text = MIMEText(
            textwrap.dedent(f"""\
            NAW 告警通知
            ──────────────────────────────────
            级别: {payload.level}
            标题: {payload.title}
            设备: {payload.device_name or 'N/A'} (id={payload.device_id})
            内容: {payload.body}
            """),
            "plain",
            "utf-8",
        )
        msg.attach(text)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port or 25) as srv:
            if settings.smtp_user and settings.smtp_password:
                srv.starttls()
                srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)
        logger.info("email notification sent: %s", payload.title)
    except Exception as exc:
        logger.warning("email notification failed: %s", exc)


def _send_webhook(payload: NotificationPayload, url: str) -> None:
    if not url:
        return
    try:
        from app.utils.ssrf import validate_url, SSRFProtectionError
        try:
            validated_url = validate_url(url)
        except SSRFProtectionError as exc:
            logger.warning("Webhook URL blocked by SSRF protection: %s (%s)", url, exc)
            return

        body = {
            **payload.to_dict(),
            "msgtype": "markdown",
            "markdown": {
                "title": payload.title,
                "content": f"### [{payload.level.upper()}] {payload.title}\n>{payload.body}",
            },
        }
        with httpx.Client(timeout=10) as client:
            resp = client.post(validated_url, json=body)
            resp.raise_for_status()
        logger.info("webhook notification sent: %s", payload.title)
    except Exception as exc:
        logger.warning("webhook notification failed: %s", exc)


def notify(payload: NotificationPayload) -> None:
    """Fan out to all configured channels."""
    _broadcast_ws(payload)
    _send_email(payload)

    if settings.webhook_url:
        _send_webhook(payload, settings.webhook_url)
    if settings.dingtalk_webhook_url:
        _send_webhook(payload, settings.dingtalk_webhook_url)
