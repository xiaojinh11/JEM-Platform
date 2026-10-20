"""Audit log helper."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.audit import AuditEvent


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Persist an audit event (no commit — caller controls the transaction)."""
    try:
        ev = AuditEvent(
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail,
            ip_address=ip_address,
        )
        db.add(ev)
        logger.info("audit actor={} action={} target={}/{} ip={}", actor, action, target_kind, target_id, ip_address)
    except Exception as exc:  # noqa: BLE001 - audit must not crash the operation
        logger.warning("audit record failed: {}", exc)
