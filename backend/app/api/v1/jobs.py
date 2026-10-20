"""Job management API."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.core.enums import JobStatus
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
from pydantic import BaseModel
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


def _check_job_access(job: Job, user: User, action: str, db: Session) -> None:
    """Raise 403 if user cannot perform action on job.

    Admin bypasses all ownership checks.
    Operators can only touch their own jobs.
    """
    if user.role == "admin":
        return
    if job.initiated_by != user.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无权{action}他人创建的 job {job.id}",
        )


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Job).order_by(Job.created_at.desc()).limit(100)
    # Non-admin users only see their own jobs
    if user.role != "admin":
        query = query.filter(Job.initiated_by == user.username)
    return query.all()


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    _check_job_access(job, user, "查看", db)
    return job


@router.post("/dry-run", response_model=DryRunOut)
def dry_run_job(
    payload: DryRunRequest,
    _user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Render + risk-check a would-be job without creating it.

    Routes both legacy and new ``plan`` forms through a single
    ``ConfigPlan`` object so the preview sees *exactly* what the Worker
    will execute. LLM risk comment is included only when rules flag
    something dangerous, so a routine dry-run stays cheap.
    """
    from app.services.plan_builder import (
        assess_plan_risk,
        build_plan_from_inputs,
        build_plan_from_template,
    )

    plan = None
    try:
        if payload.plan is not None:
            # New form: client already built a ConfigPlan. Trust it.
            plan = ConfigPlan.from_storage_dict(payload.plan)
        elif payload.template_id is not None:
            plan = build_plan_from_template(
                db,
                template_id=payload.template_id,
                params=payload.params,
                device_ids=payload.device_ids,
            )
        elif payload.commands:
            plan = build_plan_from_inputs(
                commands=payload.commands,
                device_ids=payload.device_ids,
                meta=dict(payload.params or {}),
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="commands / template_id / plan 至少需要一个",
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Phase 1 — rule verdict (always).
    plan_risk = assess_plan_risk(db, plan, use_llm=False)

    # Phase 2 — LLM augment (only when dangerous; never blocks).
    if plan.risk.level == "dangerous":
        try:
            assess_plan_risk(db, plan, use_llm=True)
            plan_risk = plan.risk
        except Exception as exc:  # noqa: BLE001
            logger.warning("dry-run LLM augment failed: {}", exc)

    # Render legacy DryRunOut shape so existing frontends stay green.
    per_device = [
        {"device_id": did, "commands": list(plan.commands)}
        for did in plan.device_ids
    ]
    unique_lines = list(dict.fromkeys(plan.commands))
    per_dev_sec = 30 + max(1, len(unique_lines))
    total_minutes = max(1, round((per_dev_sec * len(per_device)) / 60))
    notes: list[str] = []
    if not plan.device_ids:
        notes.append("未选择目标设备，dry-run 仅展示渲染结果")
    if plan.risk.level == "dangerous":
        notes.append(
            f"检测到 {len(plan.risk.matched)} 条高危命令，需要管理员审批"
        )
    if plan.device_ids and len(plan.device_ids) > 50:
        notes.append(
            f"目标设备 {len(plan.device_ids)} 台，建议拆分批次执行"
        )

    return {
        "job_kind": "template_apply" if plan.source.source == "template"
                    else "batch_write",
        "commands_by_device": per_device,
        "risk": {
            "is_dangerous": plan.risk.level == "dangerous",
            "matched_commands": plan.risk.matched,
            "warning": "",
            "requires_approval": plan.risk.requires_approval,
            "blast_radius": plan.risk.blast_radius,
            "ai_comment": plan.risk.ai_comment,
        },
        "estimated_minutes": total_minutes,
        "notes": notes,
    }


@router.post("", response_model=JobOut, status_code=201)
def create_job(payload: JobCreate, _user=Depends(require_operator), db: Session = Depends(get_db)):
    # Rate limit: max 500 devices per job (prevent worker exhaustion)
    if len(payload.target_device_ids) > 500:
        raise HTTPException(
            status_code=400,
            detail=f"Device count {len(payload.target_device_ids)} exceeds limit 500. Split into smaller batches.",
        )

    # Per-user throttle on batch-job creation. A user can't queue more
    # than 3 batch jobs per 10 minutes — each 500-device job holds a
    # worker thread for ~30 minutes, so without this cap it's easy to
    # monopolize the queue with one click-happy operator.
    from app.core.ratelimit import batch_job_user_limiter
    allowed, retry_after = batch_job_user_limiter.check(str(_user.id))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"批量任务提交过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    # ── Resolve plan (new dual-shape) ────────────────────────────────
    # Accept either a pre-built ConfigPlan (``payload.plan``) or legacy
    # (``template_id`` / ``commands``) form. The plan is what the Worker
    # will execute; legacy fields are kept on the row for backward compat.
    from app.services.plan_builder import (
        assess_plan_risk,
        build_plan_from_inputs,
        build_plan_from_template,
    )
    plan: ConfigPlan
    legacy_params: dict = dict(payload.params or {})
    if payload.plan is not None:
        plan = ConfigPlan.from_storage_dict(payload.plan)
        # Reconcile target_device_ids — the plan carries them already,
        # but the legacy field on the Job row is authoritative for the
        # worker; if the client sent both, the Job column wins.
        if payload.target_device_ids:
            plan.device_ids = list(payload.target_device_ids)
    elif payload.template_id is not None:
        try:
            plan = build_plan_from_template(
                db,
                template_id=payload.template_id,
                params=payload.params,
                device_ids=payload.target_device_ids,
            )
        except ValueError as exc:
            # ``build_plan_from_template`` raises ``ValueError`` when
            # the template id doesn't exist or the render step blows
            # up. Surface that as a 400 with the same "dry-run failed"
            # prefix the post-create re-render uses, so callers have a
            # consistent shape regardless of where the render broke.
            raise HTTPException(
                status_code=400,
                detail=f"dry-run failed: {exc}",
            ) from exc
    elif legacy_params.get("__inline_commands"):
        # Back-compat: frontends that stash commands under the magic key.
        plan = build_plan_from_inputs(
            commands=legacy_params["__inline_commands"].splitlines(),
            device_ids=payload.target_device_ids,
            meta=legacy_params,
        )
    elif payload.kind in ("batch_read", "collection"):
        # Read-only / collection jobs don't need a full plan. But the
        # operator almost always wants to specify which commands to run
        # (display current-configuration, display version, etc.) — see
        # the JobsPage UI bug where this field was missing entirely and
        # the worker silently fell back to the hardcoded default.
        #
        # The accepted sources of truth for the command list, in order:
        #   1. ``payload.commands``  — the new explicit field on the
        #      create-task form.  Most reliable; the UI sends one
        #      command per line.
        #   2. ``legacy_params['__inline_commands']`` — older clients
        #      that pre-build the command list as a single newline-
        #      joined string.  Preserved for backward compat.
        #   3. Empty list — operator really did mean "use defaults".
        user_cmds: list[str] = []
        if payload.commands:
            user_cmds = [c.strip() for c in payload.commands if c and c.strip()]
        if not user_cmds and isinstance(legacy_params, dict):
            inline = legacy_params.get("__inline_commands")
            if isinstance(inline, str) and inline.strip():
                user_cmds = [c.strip() for c in inline.splitlines() if c.strip()]
        if not user_cmds:
            # Default fallback for the "create empty batch_read then
            # start" path.  We surface a warning in the response so the
            # operator knows what they queued.
            user_cmds = ["display current-configuration"]
        plan = build_plan_from_inputs(
            commands=user_cmds,
            device_ids=payload.target_device_ids,
            meta=legacy_params,
        )
        plan.source.source = "manual"
    else:
        raise HTTPException(
            status_code=400,
            detail="创建任务需要 plan / template_id / params.__inline_commands 之一",
        )

    # Optional: let the client pin the source for audit (manual/AI/...).
    if payload.source:
        plan.source.source = payload.source

    # Persist the plan on Job.params['__plan__'] so the Worker, audit
    # log and rollback-plan builders can all read the same source of
    # truth that the user previewed.
    from app.schemas.plan import wraps_for_storage
    persisted_params = wraps_for_storage(plan)
    # Keep user-supplied meta keys but never let the client overwrite
    # the plan block we've just authored.
    persisted_params["__plan__"] = plan.to_storage_dict()
    # Also stash the resolved command list at the well-known key the
    # worker reads (``params['__commands__']``) — this is what
    # command_collect-style flows expect, and it lets batch_collect
    # dispatch the same path.  Without this, batch_collect had no
    # way to know which commands the operator wanted and silently
    # fell back to a hardcoded default.
    if user_cmds:
        persisted_params["__commands__"] = list(user_cmds)

    job = Job(
        name=payload.name,
        kind=payload.kind,
        target_device_ids=payload.target_device_ids,
        template_id=plan.source.template_id,
        params=persisted_params,
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
    rendered_commands = list(plan.commands)
    if not rendered_commands and payload.template_id is not None:
        # Defensive: if the plan came in empty but a template was used,
        # re-render to keep dry-run == execute parity.
        try:
            preview = dry_run(
                db,
                job_kind=job.kind,
                template_id=job.template_id,
                params=job.params,
                device_ids=job.target_device_ids,
            )
            rendered_commands = [
                c
                for entry in preview["commands_by_device"]
                for c in entry["commands"]
            ]
        except Exception as exc:  # noqa: BLE001
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


def _dispatch_celery_safe(task_obj, *args, **kwargs):
    """Run a Celery task in the background, bypassing the broker.

    The original ``task.delay(...)`` call blocks the API thread
    for the entire DNS-retry window when Redis is unreachable on
    Windows (≈30s).  That made the front-end's 30-second axios
    timeout the de-facto SLA.  We call the task function directly
    via ``task_obj.run(*args, **kwargs)`` (Celery exposes the
    wrapped function as ``.run``).  This is equivalent to what
    ``task_always_eager=True`` does internally, and avoids any
    broker / result-backend I/O.  For long-running tasks the
    caller should run this in a FastAPI ``BackgroundTasks`` /
    threadpool.
    """
    return task_obj.run(*args, **kwargs)


@router.post("/{job_id}/start")
def start_job(
    job_id: int,
    background: BackgroundTasks,
    user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    _check_job_access(job, user, "启动", db)
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

    # Schedule the work onto FastAPI's BackgroundTasks.  This runs
    # the Celery task function in a threadpool worker, NOT in the
    # request handler, so the API returns 202 immediately even
    # though ``_dispatch_celery_safe`` calls ``task.run()`` directly
    # (no broker).
    if job.kind == "batch_read":
        background.add_task(_dispatch_celery_safe, batch_collect, job.id)
    elif job.kind in {"batch_write", "template_apply"}:
        # Worker reads template_id + params directly off the Job row.
        # Passing payload args here is deprecated; the worker will ignore
        # them and log a warning if they are non-empty.
        background.add_task(_dispatch_celery_safe, batch_apply, job.id)
    else:
        background.add_task(_dispatch_celery_safe, batch_collect, job.id)

    job.celery_task_id = f"bg-{job.id}"
    job.status = "pending"
    db.commit()
    from app.services.audit import record as audit_record
    audit_record(db, actor=user.username, action="job.start", target_kind="job",
                 target_id=str(job.id), detail=f"job={job.name}")
    db.commit()
    return {"celery_task_id": job.celery_task_id, "status": "pending"}


class ApproveRequest(BaseModel):
    pass


@router.post("/{job_id}/approve")
def approve_job(job_id: int, _req: ApproveRequest, user=Depends(require_operator), db: Session = Depends(get_db)):
    """Admin-only: approve a high-risk job so it can be started."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可审批")
    job.approved_by = user.username
    job.approved_at = _utcnow()
    db.commit()
    return {"ok": True, "approved_by": job.approved_by, "approved_at": job.approved_at}


@router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Delete a job row + its steps.

    The old JobsPage UI had no 删除 button at all (it was a known gap).
    The new button calls this endpoint, which:

    1. Refuses to delete a job that's still running — the operator
       must cancel first, then delete. Deleting a live job would
       leave the worker thread with a dangling FK to a missing row.
    2. Cascades to ``job_steps`` and ``backup_snapshots`` via the
       relationship ``cascade="all, delete-orphan"`` on the Job model.
       (See ``app/models/job.py``.)
    3. Records the deletion in the audit log so "who nuked job 42"
       is still answerable after the row is gone.
    """
    from app.services.audit import record as audit_record

    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    _check_job_access(job, user, "删除", db)
    if job.status in {"running", "pending"}:
        raise HTTPException(
            status_code=409,
            detail=f"任务正在 {job.status}，请先取消后再删除",
        )

    # Snapshot fields before the row goes — audit needs them.
    snapshot = {
        "id": job.id,
        "name": job.name,
        "kind": job.kind,
        "status": job.status,
        "initiated_by": job.initiated_by,
        "target_device_ids": list(job.target_device_ids or []),
    }
    db.delete(job)
    db.commit()
    # Audit ``record()`` takes target_kind + target_id, not a single
    # ``target=`` kwarg.  Pass the snapshot as ``detail`` so the
    # "who nuked job 42" question is still answerable after the row
    # is gone.
    audit_record(
        db,
        actor=user.username,
        action="job.delete",
        target_kind="job",
        target_id=str(job_id),
        detail=json.dumps(snapshot, ensure_ascii=False),
    )
    return None


@router.post("/{job_id}/control")
def control_job(
    job_id: int,
    payload: JobControlRequest,
    user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    from app.services.audit import record as audit_record

    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    _check_job_access(job, user, "操作", db)
    if payload.action == "cancel":
        # Only running/pending jobs can be cancelled — refusing
        # already-terminal jobs prevents a cancel racing against the
        # worker's finalize and confusing the audit trail.
        if job.status in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value,
                          JobStatus.PARTIAL.value, JobStatus.CANCELED.value,
                          JobStatus.NEEDS_VERIFICATION.value}:
            raise HTTPException(
                status_code=409,
                detail=f"job already in terminal state '{job.status}'; cannot cancel",
            )
        job.status = JobStatus.CANCELED.value
        job.finished_at = job.finished_at or _utcnow()
        db.commit()
        audit_record(db, actor=user.username, action="job.cancel", target_kind="job",
                     target_id=str(job.id), detail=f"job={job.name}")
        db.commit()
        return {"ok": True, "status": job.status}
    raise HTTPException(status_code=400, detail="unknown action")


@router.post("/{job_id}/rollback-plan")
def get_rollback_plan(job_id: int, user=Depends(require_operator), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    _check_job_access(job, user, "查看", db)
    try:
        plan = build_rollback_plan(db, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    job.rollback_plan = plan
    db.commit()
    return plan


@router.post("/{job_id}/ai-compare")
def ai_compare_job(job_id: int, user=Depends(require_operator), db: Session = Depends(get_db)):
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
    _check_job_access(job, user, "查看", db)

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
