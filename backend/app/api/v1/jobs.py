"""Job management API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.core.logging import logger
from app.db.session import get_db
from app.models.job import Job
from app.models.user import User
from app.schemas.job import (
    DryRunOut,
    DryRunRequest,
    JobControlRequest,
    JobCreate,
    JobDetail,
    JobOut,
    JobStepOut,
)
from app.services.ai_risk import assess_job_risk
from app.services.dry_run import (
    build_rollback_plan,
    compute_ai_comparison,
    dry_run,
)
from app.workers.tasks import batch_collect, batch_apply

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return db.query(Job).order_by(Job.created_at.desc()).limit(100).all()


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/dry-run", response_model=DryRunOut)
def dry_run_job(
    payload: DryRunRequest,
    _user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Render + risk-check a would-be job without creating it.

    LLM risk comment is included only when rules flag something dangerous,
    so a routine dry-run stays cheap.
    """
    if not payload.commands and payload.template_id is None:
        raise HTTPException(status_code=400, detail="commands 或 template_id 至少需要一个")

    try:
        preview = dry_run(
            db,
            job_kind="template_apply" if payload.template_id else "batch_write",
            commands=payload.commands,
            template_id=payload.template_id,
            params=payload.params,
            device_ids=payload.device_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Optional LLM explanation, only when rules say dangerous. Falls back
    # silently to None if AI is disabled or the LLM call fails.
    if preview["risk"]["is_dangerous"]:
        try:
            risk_full = assess_job_risk(
                db,
                commands=[
                    c
                    for entry in preview["commands_by_device"]
                    for c in entry["commands"]
                ],
                device_ids=[entry["device_id"] for entry in preview["commands_by_device"]],
                use_llm=True,
            )
            preview["risk"] = {
                "is_dangerous": risk_full["is_dangerous"],
                "matched_commands": risk_full["matched_commands"],
                "warning": risk_full["warning"],
                "requires_approval": risk_full["requires_approval"],
                "blast_radius": risk_full["blast_radius"],
                "ai_comment": risk_full["ai_comment"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("dry-run risk assess failed: {}", exc)

    return preview


@router.post("", response_model=JobOut, status_code=201)
def create_job(payload: JobCreate, _user=Depends(require_operator), db: Session = Depends(get_db)):
    # Rate limit: max 500 devices per job (prevent worker exhaustion)
    if len(payload.target_device_ids) > 500:
        raise HTTPException(
            status_code=400,
            detail=f"Device count {len(payload.target_device_ids)} exceeds limit 500. Split into smaller batches.",
        )

    job = Job(
        name=payload.name,
        kind=payload.kind,
        target_device_ids=payload.target_device_ids,
        template_id=payload.template_id,
        params=payload.params,
        note=payload.note,
        batch_size=payload.batch_size,
        initiated_by=_user.username,
        status="draft",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Best-effort AI risk assessment. Persists result + requires_approval flag,
    # and builds a rollback plan that future runs can reuse. Never blocks
    # job creation if AI is unavailable — ``risk_assessment_error`` carries
    # the message and the rule-based verdict is always saved.
    try:
        preview = dry_run(
            db,
            job_kind=job.kind,
            commands=payload.params.get("__inline_commands") if payload.params else None,
            template_id=job.template_id,
            params=payload.params,
            device_ids=job.target_device_ids,
        )
        risk = assess_job_risk(
            db,
            commands=[
                c
                for entry in preview["commands_by_device"]
                for c in entry["commands"]
            ],
            device_ids=job.target_device_ids,
            use_llm=True,
        )
        job.risk_assessment = {
            "level": "dangerous" if risk["is_dangerous"] else "safe",
            "factors": [
                {"matched": cmd} for cmd in risk["matched_commands"][:20]
            ],
            "ai_comment": risk["ai_comment"],
            "blast_radius": risk["blast_radius"],
            "warning": risk["warning"],
        }
        job.risk_assessed_at = _utcnow()
        job.requires_approval = risk["requires_approval"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk assessment failed for job {}: {}", job.id, exc)
        job.risk_assessment_error = str(exc)[:1000]
        job.risk_assessed_at = _utcnow()

    try:
        job.rollback_plan = build_rollback_plan(db, job_id=job.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rollback plan build failed for job {}: {}", job.id, exc)

    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/start")
def start_job(job_id: int, _user=Depends(require_operator), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in {"draft", "pending", "canceled"}:
        raise HTTPException(status_code=409, detail="job already started")

    # Approval gate: block high-risk jobs without admin approval
    if job.requires_approval and not job.approved_by:
        raise HTTPException(
            status_code=403,
            detail="⚠️  This job contains HIGH-RISK commands and requires admin approval before execution.",
        )

    if job.kind == "batch_read":
        task = batch_collect.delay(job.id)
    elif job.kind in {"batch_write", "template_apply"}:
        # Worker pulls template+params off the Job row; passing empty
        # strings here keeps backward compatibility.
        task = batch_apply.delay(job.id, "", {})
    else:
        task = batch_collect.delay(job.id)
    job.celery_task_id = task.id
    job.status = "pending"
    db.commit()
    return {"celery_task_id": task.id, "status": "pending"}


@router.post("/{job_id}/control")
def control_job(job_id: int, payload: JobControlRequest, _user=Depends(require_operator), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if payload.action == "cancel":
        job.status = "canceled"
        db.commit()
        return {"ok": True}
    raise HTTPException(status_code=400, detail="unknown action")


@router.post("/{job_id}/rollback-plan")
def get_rollback_plan(job_id: int, _user=Depends(require_operator), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        plan = build_rollback_plan(db, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    job.rollback_plan = plan
    db.commit()
    return plan


@router.post("/{job_id}/ai-compare")
def ai_compare_job(job_id: int, _user=Depends(require_operator), db: Session = Depends(get_db)):
    """Compute expected-vs-actual comparison after the job has run.

    Pulls the LLM summary (best-effort) and persists the structured
    comparison on the Job row. Safe to call on a running job — it only
    looks at snapshots already on disk.
    """
    from app.models.ai import AIConfig
    from app.services.ai import _decrypt_api_key, get_config

    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    ai_summary: str | None = None
    try:
        cfg: AIConfig = get_config(db)
        if cfg.enabled and _decrypt_api_key(cfg):
            # Lazy import to keep this endpoint off the AI critical path
            # when AI is disabled. Caller still gets a structured diff
            # even if LLM is unreachable.
            from app.services.ai_comparison import summarise_comparison  # type: ignore
            ai_summary = summarise_comparison(db, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI comparison summary failed for {}: {}", job_id, exc)

    comp = compute_ai_comparison(db, job_id=job_id, ai_summary=ai_summary)
    job.ai_comparison = comp
    job.ai_comparison_at = _utcnow()
    db.commit()
    return comp
