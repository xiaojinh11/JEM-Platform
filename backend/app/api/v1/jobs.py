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
from app.services.ai_risk import _rule_based_risk, assess_job_risk
from app.services.dry_run import (
    build_rollback_plan,
    compute_ai_comparison,
    dry_run,
)
from app.utils.command_validator import format_risk_warning
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

    # Two-phase risk assessment:
    #
    # Phase 1 — rule-based (REQUIRED).
    # The rule layer is pure regex on the rendered command list, with no
    # external dependencies, so it should never throw. Its verdict feeds
    # ``Job.requires_approval`` which is the gate ``start_job`` checks —
    # we MUST persist this even if the AI comment or graph blast-radius
    # lookup fails, otherwise a high-risk job could slip through when the
    # graph or LLM is down.
    #
    # Phase 2 — AI risk comment + blast radius (BEST-EFFORT).
    # Failure here is logged and stored in ``risk_assessment_error`` but
    # never blocks job creation or nullifies the rule verdict.
    try:
        preview = dry_run(
            db,
            job_kind=job.kind,
            commands=payload.params.get("__inline_commands") if payload.params else None,
            template_id=job.template_id,
            params=payload.params,
            device_ids=job.target_device_ids,
        )
        rendered_commands = [
            c
            for entry in preview["commands_by_device"]
            for c in entry["commands"]
        ]
    except Exception as exc:  # noqa: BLE001
        # dry_run failing is a 4xx — bad input (template missing, render
        # error). Surface it so the caller can fix the request.
        logger.warning("dry-run failed for job {}: {}", job.id, exc)
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"dry-run failed: {exc}",
        ) from exc

    # Phase 1: rule-based verdict — always persisted.
    rule_risk = _rule_based_risk(rendered_commands)
    job.risk_assessment = {
        "level": "dangerous" if rule_risk.is_dangerous else "safe",
        "factors": [{"matched": cmd} for cmd in rule_risk.matched_patterns[:20]],
        "ai_comment": None,
        "blast_radius": {},
        "warning": format_risk_warning(rule_risk) if rule_risk.is_dangerous else "",
        "phase": "rule",  # overwritten by phase 2 if AI succeeds
    }
    job.requires_approval = rule_risk.require_approval
    job.risk_assessed_at = _utcnow()
    job.risk_assessment_error = None

    # Phase 2: AI comment + blast radius — best-effort, never blocks.
    try:
        ai_risk = assess_job_risk(
            db,
            commands=rendered_commands,
            device_ids=job.target_device_ids,
            use_llm=True,
        )
        # Merge the AI-only fields on top of the rule verdict. We do NOT
        # let the LLM downgrade ``requires_approval`` — that's rule-only.
        job.risk_assessment = {
            **job.risk_assessment,
            "ai_comment": ai_risk.get("ai_comment"),
            "blast_radius": ai_risk.get("blast_radius", {}),
            "phase": "rule+ai",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI risk assessment failed for job {}: {}", job.id, exc)
        job.risk_assessment_error = str(exc)[:1000]

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

    # Hard rule-layer gate: the rule verdict is ALWAYS written at
    # create time (it has no external dependencies). If it's missing
    # we treat the job as un-assessed and refuse to start — this is a
    # defence-in-depth check for any future code path that forgets
    # to populate the verdict before reaching this endpoint.
    if not job.risk_assessment:
        raise HTTPException(
            status_code=409,
            detail="⚠️  Job has no risk assessment — refusing to start. "
            "Re-create the job so the rule-layer verdict is populated.",
        )

    # Approval gate: block high-risk jobs without admin approval. The
    # rule layer is authoritative for ``requires_approval``; the AI
    # comment can fail without affecting this gate.
    if job.requires_approval and not job.approved_by:
        raise HTTPException(
            status_code=403,
            detail="⚠️  This job contains HIGH-RISK commands and requires admin approval before execution.",
        )

    if job.kind == "batch_read":
        task = batch_collect.delay(job.id)
    elif job.kind in {"batch_write", "template_apply"}:
        # Worker reads template_id + params directly off the Job row.
        # Passing payload args here is deprecated; the worker will ignore
        # them and log a warning if they are non-empty.
        task = batch_apply.delay(job.id)
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
