"""Dashboard overview API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.enums import JobStatus, Vendor
from app.db.session import get_db
from app.models.device import Device
from app.models.job import Job
from app.models.user import User
from app.models.credential import Credential
from app.models.document import Document
from app.models.audit import AuditEvent
from app.models.config import ConfigRevision

router = APIRouter()


class DashboardStats(BaseModel):
    total_devices: int
    active_devices: int
    simulated_devices: int
    by_vendor: dict[str, int]
    total_jobs: int
    jobs_by_status: dict[str, int]
    total_users: int
    total_credentials: int
    total_documents: int
    revisions_total: int
    pending_approval: int
    running_jobs: int


class RecentEvent(BaseModel):
    id: int
    actor: str
    action: str
    target_kind: str | None = None
    target_id: str | None = None
    detail: str | None = None
    created_at: str


class ActiveJob(BaseModel):
    id: int
    name: str
    kind: str
    status: str
    initiated_by: str
    started_at: str | None = None
    progress: int = 0
    target_device_ids: list[int] = []
    summary: dict | None = None
    requires_approval: bool = False


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    total = db.query(func.count(Device.id)).scalar() or 0
    active = db.query(func.count(Device.id)).filter(Device.is_active.is_(True)).scalar() or 0
    simulated = db.query(func.count(Device.id)).filter(Device.is_simulated.is_(True)).scalar() or 0

    vendor_rows = db.query(Device.vendor, func.count(Device.id)).group_by(Device.vendor).all()
    by_vendor = {v: c for v, c in vendor_rows}

    total_jobs = db.query(func.count(Job.id)).scalar() or 0
    status_rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    jobs_by_status = {s: c for s, c in status_rows}

    total_users = db.query(func.count(User.id)).scalar() or 0
    total_creds = db.query(func.count(Credential.id)).scalar() or 0
    total_docs = db.query(func.count(Document.id)).scalar() or 0
    revisions_total = db.query(func.count(ConfigRevision.id)).scalar() or 0

    pending_approval = (
        db.query(func.count(Job.id))
        .filter(Job.requires_approval.is_(True), Job.approved_by.is_(None), Job.status.in_(["draft", "pending"]))
        .scalar()
        or 0
    )
    running_jobs = (
        db.query(func.count(Job.id))
        .filter(Job.status.in_([JobStatus.RUNNING.value, JobStatus.PENDING.value]))
        .scalar()
        or 0
    )

    return DashboardStats(
        total_devices=total,
        active_devices=active,
        simulated_devices=simulated,
        by_vendor=by_vendor,
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        total_users=total_users,
        total_credentials=total_creds,
        total_documents=total_docs,
        revisions_total=revisions_total,
        pending_approval=pending_approval,
        running_jobs=running_jobs,
    )


@router.get("/recent-events", response_model=list[RecentEvent])
def recent_events(limit: int = 12, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Most recent audit events for the dashboard feed."""
    limit = min(max(limit, 1), 50)
    rows = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        RecentEvent(
            id=r.id,
            actor=r.actor,
            action=r.action,
            target_kind=r.target_kind,
            target_id=r.target_id,
            detail=r.detail,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.get("/active-jobs", response_model=list[ActiveJob])
def active_jobs(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Currently running or pending jobs, plus recent failures for visibility."""
    rows = (
        db.query(Job)
        .filter(
            Job.status.in_(
                [
                    JobStatus.RUNNING.value,
                    JobStatus.PENDING.value,
                    JobStatus.NEEDS_VERIFICATION.value,
                ]
            )
        )
        .order_by(Job.created_at.desc())
        .limit(20)
        .all()
    )
    out: list[ActiveJob] = []
    for j in rows:
        target = list(j.target_device_ids or [])
        summary = j.summary or {}
        total = summary.get("total") or len(target)
        done = (summary.get("succeeded") or 0) + (summary.get("failed") or 0) + (summary.get("needs_verification") or 0) + (summary.get("skipped") or 0)
        progress = 0 if total == 0 else int(round(done * 100 / max(total, 1)))
        out.append(
            ActiveJob(
                id=j.id,
                name=j.name,
                kind=j.kind,
                status=j.status,
                initiated_by=j.initiated_by,
                started_at=j.started_at.isoformat() if j.started_at else None,
                progress=progress,
                target_device_ids=target,
                summary=summary,
                requires_approval=j.requires_approval,
            )
        )
    return out
