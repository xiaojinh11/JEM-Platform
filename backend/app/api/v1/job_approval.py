"""Job approval endpoints (admin-only)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.job import Job
from app.models.user import User
from app.services.audit import record as audit_record

router = APIRouter()


@router.post("/{job_id}/approve")
def approve_job(job_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin approves a high-risk job for execution."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    if not job.requires_approval:
        raise HTTPException(status_code=400, detail="job does not require approval")

    if job.approved_by:
        raise HTTPException(status_code=409, detail=f"already approved by {job.approved_by}")

    job.approved_by = admin.username
    job.approved_at = datetime.now(timezone.utc)

    audit_record(
        db,
        actor=admin.username,
        action="job.approve",
        target_kind="job",
        target_id=str(job.id),
        detail=f"Approved high-risk job: {job.name}",
    )

    db.commit()
    return {"ok": True, "approved_by": admin.username, "approved_at": job.approved_at}


@router.post("/{job_id}/reject")
def reject_job(job_id: int, reason: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin rejects a high-risk job."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    if not job.requires_approval:
        raise HTTPException(status_code=400, detail="job does not require approval")

    job.status = "rejected"
    job.note = (job.note or "") + f"\n[REJECTED by {admin.username}]: {reason}"

    audit_record(
        db,
        actor=admin.username,
        action="job.reject",
        target_kind="job",
        target_id=str(job.id),
        detail=f"Rejected: {reason}",
    )

    db.commit()
    return {"ok": True, "status": "rejected"}
