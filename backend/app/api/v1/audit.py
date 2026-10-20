"""Audit log endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.user import User

router = APIRouter()


@router.get("")
def list_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    actor: str | None = Query(None),
    action: str | None = Query(None),
    since_days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(AuditEvent)
    if actor:
        q = q.filter(AuditEvent.actor == actor)
    if action:
        q = q.filter(AuditEvent.action.like(f"{action}%"))
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    q = q.filter(AuditEvent.created_at >= since)
    return q.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset).all()
