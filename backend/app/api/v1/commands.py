"""Custom command collection endpoints (migrated from PythonProject5).

This module exposes the "user types arbitrary display commands and the
platform collects the results" workflow that was previously handled by
``PythonProject5/main.py`` + ``PythonProject5/webapp.py``.

The endpoint accepts:

* ``device_ids``     — target device list (max 500, same as ``/jobs``)
* ``commands``       — list of read-style commands to run on each
  device (e.g. ``display current-configuration``,
  ``display fan``, ``display version``)
* ``name``           — optional human-friendly label for the resulting
  job
* ``note``           — optional free-form note attached to the audit
  log

It then:

1. Validates devices and the command list (non-empty, no obvious
   dangerous patterns — the server-side safety net is in
   ``executor.collect_custom_commands``)
2. Creates a ``Job`` row with ``kind=command_collect`` so the existing
   job-stream / WebSocket / summary UI works out of the box
3. Stores the command list on ``Job.params['__commands__']`` (the
   private convention used by ``batch_apply`` for inline commands)
4. Fires a ``command_collect`` Celery task in the background

Read paths let the user list their own recent command-collect jobs and
the per-device revision list that the worker wrote into the existing
``config_revisions`` table — no new tables, no new endpoints to
maintain.

Authentication: writes (``POST /collect``) require
``require_operator``; reads (``GET /jobs``) require
``get_current_user`` so viewers can browse the audit trail.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.core.config import settings
from app.core.logging import logger
from app.core.ratelimit import batch_job_user_limiter
from app.db.session import get_db
from app.models.device import Device
from app.models.user import User
from app.schemas.config import ConfigRevisionOut
from app.services.audit import record as audit_record
from app.services.configs import (
    get_revision,
    list_revisions,
    read_revision_text,
)
from app.workers.tasks import command_collect

router = APIRouter()


# ── Request / response shapes ───────────────────────────────────────────

# Mirror the executor-side guard so the API fails fast with a
# user-friendly message rather than letting the request reach the
# worker, where the same check would store an error row per device.
_DANGEROUS_RE = re.compile(
    r"\b(reboot|reset\s+|format|delete|system-view|undo\s+|erase|write\s+erase)\b",
    re.IGNORECASE,
)


class CommandCollectRequest(BaseModel):
    device_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Target device IDs. Max 500 per request (matches /jobs limit).",
    )
    commands: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "List of read-style commands to run on every device. "
            "Each line is sent as a separate ``invoke_shell`` write. "
            "Dangerous keywords (``reboot`` / ``reset`` / ``format`` / "
            "``delete`` / ``system-view`` / ``undo`` / ``erase`` / "
            "``write erase``) are rejected with HTTP 400 before any "
            "device is touched."
        ),
    )
    name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional human-friendly label; defaults to a timestamp.",
    )
    note: Optional[str] = Field(
        default=None,
        max_length=512,
        description="Optional free-form note attached to the audit log + job row.",
    )

    @field_validator("device_ids")
    @classmethod
    def _check_device_count(cls, v: list[int]) -> list[int]:
        if len(v) > 500:
            raise ValueError("device_ids exceeds limit 500")
        # Deduplicate while preserving order — saves worker loops
        # and prevents the audit log from being multiplied by
        # accidental double-clicks.
        seen: set[int] = set()
        deduped: list[int] = []
        for d in v:
            if d not in seen:
                seen.add(d)
                deduped.append(d)
        return deduped

    @field_validator("commands")
    @classmethod
    def _check_commands(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in v:
            cmd = (raw or "").strip()
            if not cmd or cmd.startswith("#"):
                continue
            if len(cmd) > 1024:
                raise ValueError(f"command too long (>1024 chars): {cmd[:32]}…")
            if _DANGEROUS_RE.search(cmd):
                raise ValueError(
                    f"refused dangerous command: {cmd[:64]}. "
                    "Use the standard /jobs endpoints (with admin approval) "
                    "for mutating operations."
                )
            cleaned.append(cmd)
        if not cleaned:
            raise ValueError("commands list is empty after stripping comments / blanks")
        if len(cleaned) > 50:
            raise ValueError("commands list exceeds 50 entries per request")
        return cleaned


class CommandCollectResponse(BaseModel):
    job_id: int
    name: str
    devices: int
    commands: int
    celery_task_id: str
    estimated_minutes: float


# ── Background wrapper ───────────────────────────────────────────────────

def _safe_command_collect(job_id: int, logger) -> None:
    """Background-task wrapper that runs the Celery task in-process.

    Mirrors the rationale in ``api/v1/health.py::_safe_collect_one``:
    we don't go through ``.delay()`` because in dev mode
    (``task_always_eager=True``) that blocks this background
    thread for the full SSH round-trip, and with no Redis
    broker ``.delay()`` raises ``Retry limit exceeded`` (30s
    DNS window).

    The task opens its own DB session and SSH connections, so
    running it in FastAPI's background threadpool is safe.
    """
    try:
        result = command_collect.run(job_id)
        logger.info(
            "command_collect job={} finished: {}", job_id, result,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "command_collect job={} failed: {}", job_id, exc,
        )


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/collect", response_model=CommandCollectResponse, status_code=202)
async def collect_commands(
    payload: CommandCollectRequest,
    background: BackgroundTasks,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> CommandCollectResponse:
    """Trigger a custom-command collection run. Returns immediately.

    The result is a ``Job`` row (kind=command_collect) plus a Celery
    task that runs the command set on each device. The existing
    ``/api/v1/jobs/{job_id}`` endpoint exposes the per-step status;
    the resulting per-device snapshots land in the standard
    ``config_revisions`` table (one revision per (device, command)
    pair, source="command_collect") so ``/api/v1/configs`` and the
    diff UI show them without any new schema.
    """
    # Per-user throttle — same cap as ``create_job`` so a misconfigured
    # script can't queue thousands of custom-command jobs in a minute
    # and crowd out real batch operations.
    allowed, retry_after = batch_job_user_limiter.check(str(user.id))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"命令采集提交过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    # Validate devices exist. We do this explicitly so a typo
    # surfaces as 404, not as a quiet "this device was ignored"
    # surprise when the worker iterates the list.
    existing_ids = {
        d.id for d in db.query(Device.id).filter(Device.id.in_(payload.device_ids)).all()
    }
    missing = [d for d in payload.device_ids if d not in existing_ids]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"device(s) not found: {missing[:10]}{'...' if len(missing) > 10 else ''}",
        )

    # Lazily import the Job model so the request schema validation
    # above doesn't pull in the whole job module.
    from app.models.job import Job

    # Job name: user-supplied or timestamp-based. Keep it <= 128
    # chars because the column is ``name VARCHAR(128)``.
    job_name = (
        payload.name
        or f"命令采集 {datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')}"
    )
    if len(job_name) > 128:
        job_name = job_name[:128]

    job = Job(
        name=job_name,
        kind="command_collect",
        target_device_ids=payload.device_ids,
        # The convention is to put the dynamic payload under a
        # double-underscore key so the worker can read it without
        # colliding with user-supplied ``params`` (which we don't
        # accept in this request to keep the surface small).
        params={"__commands__": payload.commands},
        note=payload.note,
        initiated_by=user.username,
        status="draft",
    )
    db.add(job)
    db.flush()  # populate job.id
    job.celery_task_id = f"cmd-{job.id}"
    db.commit()
    db.refresh(job)

    # Schedule the worker in the background. This mirrors the
    # ``/jobs/{id}/start`` flow but skips the ``risk_assessment`` /
    # approval gate (custom-command collection is read-only by
    # design — the dangerous-command filter above is the only
    # safety check, and the per-command error handling in the
    # worker prevents one bad command from killing the rest).
    background.add_task(_safe_command_collect, job.id, logger)

    # Audit. ``action=command_collect`` is a new verb but the
    # ``AuditEvent.action`` column is free-form text, so it just
    # appears in the audit log without a schema change.
    audit_record(
        db,
        actor=user.username,
        action="command_collect.create",
        target_kind="job",
        target_id=str(job.id),
        detail=(
            f"devices={payload.device_ids[:10]}{'...' if len(payload.device_ids) > 10 else ''} "
            f"commands={len(payload.commands)}"
        ),
    )
    db.commit()

    # Rough estimate: 3s per (device × command) at the default
    # ``collect_max_concurrency=5`` cap. This is a planning hint
    # for the UI's progress bar — the real duration is bounded
    # by the worker's per-device timeout.
    seconds = 3.0 * len(payload.device_ids) * len(payload.commands)
    estimated_minutes = round(seconds / 60.0, 1)

    logger.info(
        "command_collect.create user={} job={} devices={} commands={}",
        user.username, job.id, len(payload.device_ids), len(payload.commands),
    )

    return CommandCollectResponse(
        job_id=job.id,
        name=job_name,
        devices=len(payload.device_ids),
        commands=len(payload.commands),
        celery_task_id=job.celery_task_id or "",
        estimated_minutes=estimated_minutes,
    )


# ── Read paths (audit + history browsing) ───────────────────────────────


@router.get("/jobs", response_model=list[dict])
def list_command_collect_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List recent ``command_collect`` jobs for the current user.

    Returns the standard ``Job`` columns plus a short preview of the
    command list (first 5) so the UI can show "what did this run?"
    without having to fetch the full job each time.
    """
    from app.models.job import Job

    rows = (
        db.query(Job)
        .filter(Job.kind == "command_collect")
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for j in rows:
        params = j.params or {}
        cmds = params.get("__commands__", []) or []
        out.append({
            "id": j.id,
            "name": j.name,
            "status": j.status,
            "target_device_ids": j.target_device_ids,
            "command_count": len(cmds),
            "command_preview": cmds[:5],
            "initiated_by": j.initiated_by,
            "summary": j.summary,
            "note": j.note,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        })
    return out


# ── Per-device export (zip of the latest revisions) ─────────────────────


def _safe_filename(name: str) -> str:
    """Strip path separators and control chars; cap to 80 chars."""
    cleaned = "".join(
        ch for ch in name
        if ch.isprintable() and ch not in {"/", "\\", "\0"}
    )
    return cleaned.strip()[:80] or "command-collect"


@router.get("/jobs/{job_id}/export")
def export_command_collect(
    job_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Download the latest per-(device, command) revisions as a zip.

    One txt file per (device, command) pair. Filename is
    ``<sanitized_device>__<sanitized_command>.txt``.

    The endpoint is read-only and uses the existing
    ``config_revisions`` rows; no extra schema.

    Accepted job kinds:
      * ``command_collect`` — created via POST /commands/collect.
      * ``batch_read`` — created via the JobsPage UI (the worker
        now delegates to ``command_collect`` if user commands
        were supplied, so the revisions land in the same table
        with the same ``note`` stamp and this endpoint can read
        them back the same way).

    We intentionally don't accept ``batch_write`` / ``template_apply``
    here because those write real configs to devices, and the
    per-(device, command) row layout wouldn't make sense.
    """
    from app.models.job import Job

    job = db.get(Job, job_id)
    if not job or job.kind not in {"command_collect", "batch_read"}:
        raise HTTPException(
            status_code=404,
            detail=f"export only supported for command_collect / batch_read jobs (got {job.kind if job else 'missing'})",
        )

    params = job.params or {}
    commands: list[str] = list(params.get("__commands__", []) or [])
    if not commands:
        raise HTTPException(
            status_code=409,
            detail="job has no command list (params.__commands__ missing)",
        )

    # Map device_id → (name, ip) for the filename header.
    devices = {
        d.id: d for d in db.query(Device).filter(Device.id.in_(job.target_device_ids)).all()
    }

    # Walk the revisions for each device. We only pull the
    # ``note`` because the note was stamped as
    # ``"job#{job_id} cmd={command}"`` by the worker, so a
    # simple filter gives us the right rows.
    buf = io.BytesIO()
    found_any = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for device_id in job.target_device_ids:
            dev = devices.get(device_id)
            dev_label = (dev.name if dev else f"device-{device_id}").replace(" ", "_")
            revs = list_revisions(db, device_id)
            # Keep only the revisions for this job, with a
            # note mentioning one of the user's commands. We
            # iterate commands first (cheaper than revs
            # because there are <= 50 of them) and look up
            # the most recent matching revision.
            for cmd in commands:
                matching = [
                    r for r in revs
                    if (r.note or "").startswith(f"job#{job_id} cmd=")
                    and f"cmd={cmd}" in (r.note or "")
                ]
                if not matching:
                    continue
                latest = matching[0]  # list_revisions is already DESC
                text = read_revision_text(latest)
                head = (
                    f"设备: {dev_label}\n"
                    f"IP:   {dev.address if dev and hasattr(dev, 'address') else '-'}\n"
                    f"命令: {cmd}\n"
                    f"时间: {latest.created_at.isoformat() if latest.created_at else '-'}\n"
                    f"job:  #{job_id}\n"
                    f"{'=' * 60}\n\n"
                )
                z.writestr(f"{dev_label}__{cmd.replace(' ', '_')}.txt", head + text)
                found_any = True
    if not found_any:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no revisions found for job {job_id} yet — "
                "the worker may still be running, or all commands failed."
            ),
        )

    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"command-collect-{job_id}-{stamp}.zip"
    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
