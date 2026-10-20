"""Celery tasks for batch read/write/backup."""
from __future__ import annotations

import time
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import JobStatus, JobStepKind, Vendor
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.device import Device, DeviceEndpoint
from app.models.credential import Credential
from app.models.job import Job, JobStep, BackupSnapshot
from app.models.template import Template
from app.models.health import DeviceHealthSnapshot
from app.services.executor import (
    collect_config,
    apply_config,
    ExecutorError,
    collect_custom_commands,
)
from app.services.configs import store_snapshot, import_text
from app.services.health_collector import (
    HealthSnapshot,
    collect_health_via_executor,
)
from app.services.job_stream import publish_sync as publish_job_event
from app.services.templates import render, extract_commands
from app.workers import celery_app

# Concurrency limits for SSH operations.
#
# NOTE: This is now sourced from ``settings.collect_max_concurrency`` so
# operators can tune per deployment without a code change. The default
# of 5 was chosen because running more than ~10 simultaneous SSH
# handshakes against Huawei VRP / Cisco IOS-XE typically triggers
# "sshd: authentication failure" storms that brick the box's control
# plane. If you need higher throughput, scale out Celery workers
# horizontally (each worker is its own process — true multiprocessing)
# rather than raising this number.
DEFAULT_MAX_SSH_CONCURRENCY = 5


def _utcnow() -> datetime:
    """Return current UTC time with timezone info (Python 3.12+ safe)."""
    return datetime.now(timezone.utc)


def _job_was_canceled(db: Session, job: Job) -> bool:
    """Re-fetch the Job row and report whether the operator asked to cancel.

    The main worker thread polls this between device iterations; the per-
    device helpers (in thread workers) call the same check before they
    open an SSH session so we don't dispatch a long-running write after
    cancel. We deliberately refresh instead of trusting the in-memory
    ``job`` so concurrent ``/control cancel`` writes from the API are
    seen promptly. The check is cheap (single ``SELECT`` on a primary
    key) so polling it before each device is fine.
    """
    db.expire(job)
    fresh = db.get(Job, job.id)
    return fresh is not None and fresh.status == JobStatus.CANCELED.value


def _summarize(steps: Iterable[JobStep]) -> dict:
    statuses = [s.status for s in steps]
    return {
        "total": len(statuses),
        "succeeded": sum(1 for s in statuses if s == "succeeded"),
        "failed": sum(1 for s in statuses if s == "failed"),
        "needs_verification": sum(1 for s in statuses if s == "needs_verification"),
        "skipped": sum(1 for s in statuses if s == "skipped"),
    }


def _resolve_endpoint(db: Session, device_id: int) -> tuple[Device, DeviceEndpoint | None, Credential | None]:
    dev = db.get(Device, device_id)
    if not dev:
        raise RuntimeError("device not found")
    ep = (
        db.query(DeviceEndpoint)
        .filter_by(device_id=device_id, is_primary=True)
        .first()
        or db.query(DeviceEndpoint).filter_by(device_id=device_id).first()
    )
    if not ep:
        return dev, None, None
    cred = db.get(Credential, ep.credential_id) if ep.credential_id else None
    return dev, ep, cred


def _require_real_transport(
    dev: Device,
    ep: Optional[DeviceEndpoint],
    cred: Optional[Credential],
    *,
    action: str,
) -> None:
    """Guard that the device has a real SSH transport available.

    Used by every worker path (config collect, config apply, health
    snapshot) to refuse the legacy "synthetic success" branch: if the
    device is marked simulated, has no endpoint, or has an endpoint
    without a credential, we raise a ``RuntimeError`` carrying a
    remediation hint so the operator can see *why* it failed instead
    of a fake success row polluting the dashboards.

    Args:
        dev: the device row (already resolved).
        ep:  the primary endpoint (may be None).
        cred: the credential the endpoint points to (may be None).
        action: short human label for the worker step that's
            refusing — included in the error message so the audit
            trail says e.g. "apply: device 5 has is_simulated=True".
    """
    if dev.is_simulated:
        raise RuntimeError(
            f"{action}: device {dev.id} ({dev.name}) has is_simulated=True — "
            f"real SSH transport required; edit the device to use a real "
            f"vendor (huawei_vrp / cisco_ios_xe / ruijie_rgos) before retrying"
        )
    if not ep:
        raise RuntimeError(
            f"{action}: device {dev.id} ({dev.name}) has no primary endpoint — "
            f"add an SSH endpoint (POST /api/v1/devices/{dev.id}/endpoints) "
            f"and mark it primary before retrying"
        )
    if not cred:
        raise RuntimeError(
            f"{action}: device {dev.id} ({dev.name}) endpoint {ep.id} has no "
            f"credential_id — bind a credential via PATCH /endpoints/{ep.id} "
            f"or re-create the endpoint with credential_id set"
        )


def _load_template_for_job(db: Session, job: Job) -> tuple[str | None, dict]:
    """Return (template_text, params) for a job by reading Job.template_id.

    Inline commands (no template) are supported via
    ``job.params["__inline_commands"]`` (already populated by
    ``services.dry_run``). Returns ``(None, {})`` and writes a failed
    PRECHECK step per device if the template is missing or empty.

    Path 0 (preferred): ``job.params['__plan__']`` — a ConfigPlan written
    by the API at job-create time. This guarantees that the Worker runs
    exactly the commands the user previewed during Dry-Run. We trust the
    plan's command list as the source of truth and only fall through to
    the legacy paths when no plan is present (older rows / clients).
    """
    params = dict(job.params or {})

    # Path 0: ConfigPlan. The API writes the rendered commands
    # side-by-side (in ``__inline_commands`` for backward compat with
    # this same function) so we get preview == execute parity. We
    # re-resolve from the plan dict so ``meta`` and ``source`` stay in
    # sync for the audit log.
    plan_block = params.pop("__plan__", None)
    if isinstance(plan_block, dict) and plan_block.get("commands"):
        # The plan's command list *is* the rendered template body in
        # the dry-run == execute sense. Format it the same way
        # ``services.dry_run`` did: each command on its own line.
        inline_body = "\n".join(plan_block["commands"])
        if inline_body.strip():
            return inline_body, params

    # Path 1: inline commands stored under a private key.
    inline = params.pop("__inline_commands", None)
    if inline and isinstance(inline, str):
        return inline, params

    # Path 2: template_apply — body comes from the Template table.
    if job.template_id is not None:
        tpl = db.get(Template, job.template_id)
        if tpl is None or not (tpl.body or "").strip():
            err = (
                f"template_id={job.template_id} not found or has empty body"
            )
            for device_id in job.target_device_ids:
                _record_step(
                    db,
                    job,
                    device_id,
                    JobStepKind.PRECHECK.value,
                    "failed",
                    error=err,
                )
            db.commit()
            return None, {}
        return tpl.body, params

    # No template_id AND no inline commands — fatal.
    err = "batch_apply requires template_id or params.__inline_commands"
    for device_id in job.target_device_ids:
        _record_step(
            db,
            job,
            device_id,
            JobStepKind.PRECHECK.value,
            "failed",
            error=err,
        )
    db.commit()
    return None, {}


def _finalize_job_status(db: Session, job: Job) -> None:
    """Set job terminal status based on aggregated step statuses.

    The terminal status is computed from the step outcomes *unless* the
    operator already cancelled the job, in which case we keep the
    cancelled status (the cancel signal is authoritative — the worker
    must not turn a cancelled job into "partial").
    """
    db.expire(job)
    fresh = db.get(Job, job.id)
    if fresh is not None and fresh.status == JobStatus.CANCELED.value:
        # Operator cancellation is terminal. Keep the status, just stamp
        # finished_at and refresh the summary for the audit trail.
        if not job.summary:
            job.summary = _summarize(job.steps)
        job.status = JobStatus.CANCELED.value
        job.finished_at = _utcnow()
        db.commit()
        return

    if not job.summary:
        job.summary = _summarize(job.steps)
    s = job.summary
    if s.get("failed", 0) == 0 and s.get("needs_verification", 0) == 0 and s.get("total", 0) > 0:
        job.status = JobStatus.SUCCEEDED.value
    elif s.get("succeeded", 0) == 0 and s.get("needs_verification", 0) == 0:
        job.status = JobStatus.FAILED.value
    else:
        job.status = JobStatus.PARTIAL.value
    job.finished_at = _utcnow()
    db.commit()
    # Push the terminal status to the WS bus.  Frontend listens for
    # ``status_update`` events and stops polling once the job is done.
    publish_job_event(
        job.id,
        {"type": "status_update", "status": job.status, "summary": job.summary},
    )


# ── Thread-safe worker (no shared SQLAlchemy Session) ────────────────────────
def _collect_one_device_with_limits(
    job_id: int,
    device_id: int,
    initiated_by: str,
    *,
    semaphore: threading.BoundedSemaphore,
    cancel_predicate=None,
    sleep_ms: int = 0,
) -> tuple[int, str, str | None]:
    """Per-device worker with bounded concurrency, retry, and jitter.

    The semaphore is acquired **before** opening a DB session and is
    released in the ``finally`` block — this guarantees we never exceed
    the configured in-flight device count, even on retries.

    ``cancel_predicate`` is a no-arg callable that returns ``True`` when
    the job has been cancelled. Re-checked before each retry so a long
    retry loop won't keep pulling credentials after the operator hit
    "cancel".

    Returns ``(device_id, status, error)`` where ``status`` is one of
    ``succeeded`` / ``failed`` / ``skipped`` (skipped = cancel detected
    before work began).
    """
    if cancel_predicate is not None and cancel_predicate():
        return device_id, "skipped", "cancelled before start"

    # Sleep first so a 200-device burst doesn't fork 200 threads at the
    # exact same millisecond. Cheap, no-ops on the first iteration.
    if sleep_ms > 0:
        time.sleep(random.uniform(0, sleep_ms / 1000.0))

    with semaphore:
        max_retries = settings.collect_max_retries
        backoff = settings.collect_retry_backoff_sec
        for attempt in range(max_retries + 1):
            if cancel_predicate is not None and cancel_predicate():
                return device_id, "skipped", "cancelled during retry loop"

            device_status, err = _collect_one_device_thread(
                job_id, device_id, initiated_by,
            )
            if device_status == "succeeded":
                return device_id, "succeeded", None

            if attempt >= max_retries:
                return device_id, device_status, err

            # Exponential backoff + tiny jitter: retry #1 waits ~backoff s,
            # retry #2 waits ~backoff*2 s, etc. Clamped so a misconfigured
            # backoff doesn't push us past the per-device timeout.
            wait_s = min(backoff * (2 ** attempt), 15.0)
            logger.info(
                "collect job {} device {} attempt {}/{} failed, retrying in {:.1f}s",
                job_id, device_id, attempt + 1, max_retries + 1, wait_s,
            )
            time.sleep(wait_s)

        # Unreachable but explicit so the type-checker is happy.
        return device_id, "failed", "retry loop exhausted"


def _collect_one_device_thread(job_id: int, device_id: int, initiated_by: str) -> tuple[str, str | None]:
    """Per-device worker. Each call gets its own DB session.

    Returns ``(status, error)`` — the device id is implicit. Errors are
    stringified with ``[:1000]`` cap to match the previous contract.
    """
    thread_db: Session = SessionLocal()
    try:
        dev, ep, cred = _resolve_endpoint(thread_db, device_id)
        # No stub: if the device is misconfigured (simulated flag, no
        # endpoint, no credential) we report a real failure with a
        # remediation hint, instead of silently persisting a fake
        # config that looks like a successful collection.
        if dev.is_simulated:
            raise RuntimeError(
                f"device {dev.id} ({dev.name}) has is_simulated=True — "
                f"collection is disabled; edit the device to use a real vendor "
                f"(huawei_vrp / cisco_ios_xe / ruijie_rgos) before retrying"
            )
        if not ep:
            raise RuntimeError(
                f"device {dev.id} ({dev.name}) has no primary endpoint — "
                f"add an SSH endpoint (POST /api/v1/devices/{dev.id}/endpoints) "
                f"and mark it primary before retrying"
            )
        if not cred:
            raise RuntimeError(
                f"device {dev.id} ({dev.name}) endpoint {ep.id} has no "
                f"credential_id — bind a credential via PATCH /endpoints/{ep.id} "
                f"or re-create the endpoint with credential_id set"
            )
        text = collect_config(
            vendor=Vendor(dev.vendor),
            address=ep.address,
            port=ep.port,
            credential=cred,
            timeout_override=settings.collect_per_device_timeout_sec,
        )
        import_text(
            thread_db,
            device_id=device_id,
            content=text,
            author=initiated_by,
            source="collect",
            note=f"job#{job_id}",
        )
        return "succeeded", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("collect job {} device {} failed: {}", job_id, device_id, exc)
        return "failed", str(exc)[:1000]
    finally:
        thread_db.close()


@celery_app.task(
    name="naw.job.batch_collect",
    bind=True,
    max_retries=2,
    rate_limit="10/m",
    autoretry_for=(ExecutorError,),
    retry_backoff=True,
    retry_backoff_max=300,
    time_limit=600,       # hard stop: 10 min total
    soft_time_limit=540,  # gentle stop: 9 min — begin graceful cleanup first
)
def batch_collect(self, job_id: int) -> str:
    """Collect config from all devices in a job using a bounded thread pool.

    Concurrency is governed by ``settings.collect_max_concurrency``: a
    ``BoundedSemaphore`` caps the *actual* in-flight device count, so
    even if someone passes a huge device list, we never overwhelm SSH
    daemons. Each device has a per-device timeout (sum of
    ``collect_per_device_timeout_sec`` plus retry backoff windows) and
    is retried up to ``collect_max_retries`` times on transient
    failures.

    Workers are fully isolated: each thread opens its own DB session.
    Step records and final status are written by the main (Celery)
    thread.
    """
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return "missing"

        # ── P0 Security: re-verify approval before executing ─────────────
        # batch_collect reads config (no write) but may expose sensitive data;
        # require approval consistently with batch_apply.
        if job.requires_approval and not job.approved_by:
            job.status = JobStatus.FAILED.value
            job.note = "HIGH_RISK job: approval required but not granted"
            job.finished_at = _utcnow()
            db.commit()
            return "approval_required"

        # ── Custom-command delegation ─────────────────────────────────
        # The JobsPage UI bug: ``create_job`` for kind=batch_read used
        # to always build a plan with ``commands=[]``, so the only
        # command batch_collect ever ran was the hardcoded
        # ``display current-configuration`` — the operator's actual
        # command list was silently discarded.
        #
        # The fix: when the persisted plan carries a non-empty
        # ``params['__commands__']``, we delegate to ``command_collect``
        # (which already implements user-command collection end-to-end
        # and writes per-(device, command) rows to config_revisions).
        # That gives batch_read the same behavior as the working
        # ``POST /commands/collect`` path, with no duplicated
        # concurrency/SSH code.
        params = job.params or {}
        user_cmds = list(params.get("__commands__", []) or [])
        if job.kind == "batch_read" and user_cmds:
            logger.info(
                "batch_collect job {}: delegating to command_collect ({} cmds × {} devs)",
                job_id, len(user_cmds), len(job.target_device_ids or []),
            )
            db.close()
            return command_collect.run(job_id)

        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or _utcnow()
        # Snapshot primitives before threads start; never pass the Session to threads.
        job_id_val = job.id
        initiated_by = job.initiated_by
        target_ids = list(job.target_device_ids)
        started_at = job.started_at
        db.commit()

        # Concurrency knob. Clamp to >=1 so a misconfigured
        # ``collect_max_concurrency=0`` doesn't deadlock the pool.
        max_conc = max(1, settings.collect_max_concurrency)
        semaphore = threading.BoundedSemaphore(max_conc)
        jitter_ms = max(0, settings.collect_jitter_ms)

        # Helper to check cancellation against the same DB row the API
        # would update. Captures ``db`` so we can re-fetch.
        def _cancel_predicate() -> bool:
            try:
                return _job_was_canceled(db, job)
            except Exception:  # noqa: BLE001
                # Behave conservatively: if we cannot verify, allow work
                # to continue rather than silently aborting a long job.
                return False

        # Bounded thread pool. Pool size >= concurrency so we have
        # workers ready when the semaphore releases; the semaphore is
        # what actually throttles SSH connections, not the pool size.
        pool_workers = min(max(len(target_ids), max_conc), 32)
        results: list[tuple[int, str, str | None]] = []
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            futures = {
                pool.submit(
                    _collect_one_device_with_limits,
                    job_id_val,
                    dev_id,
                    initiated_by,
                    semaphore=semaphore,
                    cancel_predicate=_cancel_predicate,
                    sleep_ms=jitter_ms,
                ): dev_id
                for dev_id in target_ids
            }
            per_device_budget = (
                settings.collect_per_device_timeout_sec
                + (settings.collect_max_retries + 1)
                * min(settings.collect_retry_backoff_sec * 4, 15.0)
                + 5.0
            )
            for future in as_completed(futures, timeout=None):
                dev_id = futures[future]
                try:
                    results.append(future.result(timeout=per_device_budget))
                except FuturesTimeoutError:
                    logger.warning(
                        "collect job {} device {} exceeded per-device budget ({}s); marking failed",
                        job_id_val, dev_id, per_device_budget,
                    )
                    results.append((dev_id, "failed", "per-device budget exceeded"))
                except Exception as exc:  # noqa: BLE001
                    results.append((dev_id, "failed", str(exc)[:1000]))

        # Reload job and write all step records from the main (Celery) thread.
        db.expire(job)
        job = db.get(Job, job_id)
        for dev_id, status, error in results:
            step = JobStep(
                job_id=job_id_val,
                device_id=dev_id,
                step_kind=JobStepKind.EXECUTE.value,
                status=status,
                error=error,
                started_at=started_at,
                finished_at=_utcnow(),
            )
            db.add(step)
        db.commit()

        job.summary = _summarize(job.steps)
        _finalize_job_status(db, job)
        return job.status
    finally:
        db.close()


@celery_app.task(
    name="naw.job.batch_apply",
    bind=True,
    max_retries=2,
    rate_limit="5/m",
    autoretry_for=(ExecutorError,),
    retry_backoff=True,
    retry_backoff_max=300,
    time_limit=1800,      # hard stop: 30 min total (500 devices @ 3.6s each)
    soft_time_limit=1680, # gentle stop: 28 min — begin graceful cleanup first
)
def batch_apply(self, job_id: int, template_text: str = "", params_json: dict | None = None) -> str:
    """Render template per device, run precheck, backup, apply, verify.

    NOTE: ``template_text`` and ``params_json`` are kept for backwards
    compatibility but are no longer authoritative — the worker now reads
    ``Job.template_id`` + ``Job.params`` directly so the API never has to
    forward payloads. Inline commands travel inside ``Job.params`` under the
    ``__inline_commands`` key (handled by ``services.dry_run``).
    """
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return "missing"

        # ── P0 Security: re-verify approval before executing ─────────────
        # Even if the API checked approval before queuing, the Celery worker
        # runs asynchronously — the job could have been canceled/revoked in the
        # meantime.  Rejecting here closes the race.
        if job.requires_approval and not job.approved_by:
            job.status = JobStatus.FAILED.value
            job.note = "HIGH_RISK job: approval was revoked or never granted before execution"
            job.finished_at = _utcnow()
            db.commit()
            return "approval_required"

        # Respect cancellation that was set before we started: if the job
        # was already canceled when we fetched it, abort immediately.
        if job.status == JobStatus.CANCELED.value:
            job.finished_at = job.finished_at or _utcnow()
            db.commit()
            return JobStatus.CANCELED.value

        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or _utcnow()
        db.commit()

        # Resolve the actual template body + params from the Job row.
        # API-supplied args are ignored; if they were ever non-empty,
        # that's a regression — log it loudly.
        if template_text or params_json:
            logger.warning(
                "batch_apply received legacy payload args (job={}, template_text_len={}, params_keys={}); "
                "ignoring in favour of Job.template_id/params",
                job_id,
                len(template_text),
                list((params_json or {}).keys()),
            )

        rendered_template, params = _load_template_for_job(db, job)
        if rendered_template is None:
            # _load_template_for_job has already written a failed PRECHECK
            # step per device; just summarise and return.
            job.summary = _summarize(job.steps)
            _finalize_job_status(db, job)
            return job.status

        for device_id in job.target_device_ids:
            # Honour cancellation: if the operator hit "cancel" while we
            # were iterating, stop dispatching to remaining devices.
            # Devices we haven't started yet get a skipped step so the
            # audit trail explains why they never ran.
            if _job_was_canceled(db, job):
                logger.info(
                    "batch_apply job_id={} cancelled mid-loop; stopping at device {}/{}",
                    job.id,
                    device_id,
                    job.target_device_ids[-1] if job.target_device_ids else "?",
                )
                _record_step(
                    db,
                    job,
                    device_id,
                    JobStepKind.PRECHECK.value,
                    "skipped",
                    error="job canceled by operator before device started",
                )
                db.commit()
                break

            dev, ep, cred = _resolve_endpoint(db, device_id)
            commands: list[str] = []
            try:
                rendered = render(rendered_template, params)
                commands = extract_commands(rendered)
            except Exception as exc:  # noqa: BLE001
                _record_step(db, job, device_id, JobStepKind.PRECHECK.value, "failed", error=str(exc)[:1000])
                db.commit()
                continue

            if not commands:
                # Empty payload is a hard failure: never claim "succeeded"
                # without actually pushing any commands.
                _record_step(
                    db,
                    job,
                    device_id,
                    JobStepKind.PRECHECK.value,
                    "failed",
                    error=(
                        "rendered template produced 0 commands "
                        "(template_id=%s, params_keys=%s)"
                    )
                    % (job.template_id, sorted((params or {}).keys())),
                )
                db.commit()
                continue

            backup_step = _record_step(db, job, device_id, JobStepKind.BACKUP_BEFORE.value, "running")
            db.commit()
            try:
                _require_real_transport(dev, ep, cred, action="backup-before")
                cfg = collect_config(
                    vendor=Vendor(dev.vendor),
                    address=ep.address,
                    port=ep.port,
                    credential=cred,
                )
                snap = store_snapshot(device_id=device_id, content=cfg, trigger="before", note=f"job#{job.id}", job_id=job.id)
                bsnap = BackupSnapshot(**snap)
                db.add(bsnap)
                backup_step.status = "succeeded"
            except Exception as exc:  # noqa: BLE001
                backup_step.status = "failed"
                backup_step.error = str(exc)[:1000]
                db.commit()
                continue
            db.commit()

            apply_step = _record_step(db, job, device_id, JobStepKind.EXECUTE.value, "running")
            db.commit()
            apply_log = ""
            try:
                _require_real_transport(dev, ep, cred, action="apply")
                log = apply_config(
                    vendor=Vendor(dev.vendor),
                    address=ep.address,
                    port=ep.port,
                    credential=cred,
                    commands=commands,
                )
                apply_step.payload = {"log_preview": log[:2000]}
                apply_step.status = "succeeded"
                apply_log = log
            except Exception as exc:  # noqa: BLE001
                apply_step.status = "failed"
                apply_step.error = str(exc)[:1000]
                # Surface the per-line failure output (whatever the executor
                # captured before the exception) so the operator can see
                # *which* command blew up — not just the wrapper exception.
                apply_log = apply_step.error or ""
            apply_step.finished_at = _utcnow()
            db.commit()
            # Re-emit the EXECUTE step with the final status + actual log
            # so WS subscribers see the device output stream inline.
            publish_job_event(
                job.id,
                {
                    "type": "log",
                    "device_id": device_id,
                    "step_kind": JobStepKind.EXECUTE.value,
                    "log": apply_log[:2000],
                    "status": apply_step.status,
                },
            )

            if apply_step.status == "succeeded":
                after_step = _record_step(db, job, device_id, JobStepKind.BACKUP_AFTER.value, "running")
                db.commit()
                try:
                    _require_real_transport(dev, ep, cred, action="backup-after")
                    cfg_after = collect_config(
                        vendor=Vendor(dev.vendor),
                        address=ep.address,
                        port=ep.port,
                        credential=cred,
                    )
                    snap_after = store_snapshot(device_id=device_id, content=cfg_after, trigger="after", note=f"job#{job.id}", job_id=job.id)
                    db.add(BackupSnapshot(**snap_after))
                    after_step.status = "succeeded"
                except Exception as exc:  # noqa: BLE001
                    after_step.status = "needs_verification"
                    after_step.error = str(exc)[:1000]
                after_step.finished_at = _utcnow()
                db.commit()

            time.sleep(0.05)

        _finalize_job_status(db, job)

        # Best-effort AI comparison. Done inline (not via a separate task)
        # because the snapshot files are already on disk and the LLM call
        # is bounded by an HTTP timeout — failure here doesn't change the
        # job's status, only the ``ai_comparison`` field.
        try:
            from app.services.dry_run import compute_ai_comparison, summarise_comparison
            ai_summary = summarise_comparison(db, job_id=job.id)
            comp = compute_ai_comparison(db, job_id=job.id, ai_summary=ai_summary)
            job.ai_comparison = comp
            job.ai_comparison_at = _utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_comparison failed for job {}: {}", job.id, exc)

        return job.status
    finally:
        db.close()


def _record_step(db: Session, job: Job, device_id: int, kind: str, status: str, *, error: str | None = None, log: str | None = None) -> JobStep:
    step = JobStep(
        job_id=job.id,
        device_id=device_id,
        step_kind=kind,
        status=status,
        started_at=_utcnow(),
        error=error,
    )
    db.add(step)
    db.flush()
    # Broadcast to any subscribed WS clients.  We do this synchronously
    # (a single Redis PUBLISH, ~sub-ms) because Celery tasks run in a
    # worker process without an event loop, and the WS endpoint is in
    # the FastAPI process.  Publishing to Redis decouples them.
    publish_job_event(
        job.id,
        {
            "type": "step_update",
            "step_id": step.id,
            "device_id": device_id,
            "step_kind": kind,
            "status": status,
            "error": error,
            "log": log[:2000] if log else None,
        },
    )
    return step


# ── Health snapshot tasks ────────────────────────────────────────────────
# Lightweight read-only tasks that grab CPU/mem/temp/fan/power from
# each device.  They share the same bounded-concurrency machinery as
# ``batch_collect`` (semaphore + retry + jitter) so an operator can
# tune both code paths with one set of ``collect_*`` settings.

def _health_one_device(
    device_id: int,
    semaphore: threading.BoundedSemaphore,
    sleep_ms: int = 0,
    metrics: Optional[set[str]] = None,
) -> HealthSnapshot:
    """Open a fresh session, take a health snapshot, persist it.

    Returns the in-memory snapshot (caller may discard it; we already
    wrote a row).  Designed to be safe to call from a thread pool —
    no shared mutable state, separate ``SessionLocal()`` per call.

    ``metrics`` is an optional set that limits which commands are
    sent to the device.  ``None`` means "all metrics" (the legacy
    contract).  See ``collect_health`` for the recognised names.
    """
    if sleep_ms > 0:
        time.sleep(random.uniform(0, sleep_ms / 1000.0))

    with semaphore:
        thread_db: Session = SessionLocal()
        try:
            dev, ep, cred = _resolve_endpoint(thread_db, device_id)
            # Health snapshots are read-only — unlike batch_collect
            # / batch_apply, there's no config to corrupt.  So we
            # *don't* call _require_real_transport(): simulated
            # devices get a synthetic row (see health_collector.py
            # collect_health_via_executor -> SIMULATED branch),
            # devices without an endpoint get an error[] row, etc.
            # The previous hard reject of simulated devices was
            # correct for write jobs but wrong for read-only health:
            # operators were seeing a useless red "采集失败" with no
            # way to populate the dashboard for the simulated fleet
            # they use for testing.
            snap = collect_health_via_executor(
                vendor=Vendor(dev.vendor),
                address=ep.address if ep else "0.0.0.0",
                port=ep.port if ep else 22,
                credential=cred,
                device_id=device_id,
            )

            # When the caller asked for a metric subset, we still
            # return the full ``snap`` but make sure unrequested
            # fields are zeroed — otherwise an "all metrics" run
            # would leave stale values on a row that's only
            # "config + optical", which is misleading in the UI.
            if metrics is not None:
                wanted = metrics
                if "cpu" not in wanted:
                    snap.cpu_pct = None
                if "mem" not in wanted:
                    snap.mem_pct = None
                if "temp" not in wanted:
                    snap.temp_c = None
                if "fan" not in wanted:
                    snap.fan_state = None
                if "power" not in wanted:
                    snap.power_w = None
                if "optical" not in wanted:
                    snap.optical = []
                if "config" not in wanted:
                    snap.config_fingerprint = ""
                    snap.config_lines = 0
                    snap.config_excerpt = ""
                snap.metrics_run = sorted(wanted)

            row = DeviceHealthSnapshot(
                device_id=snap.device_id,
                collected_at=datetime.fromisoformat(snap.collected_at),
                vendor=snap.vendor,
                cpu_pct=snap.cpu_pct,
                mem_pct=snap.mem_pct,
                temp_c=snap.temp_c,
                fan_state=snap.fan_state,
                power_w=snap.power_w,
                # Tag simulated devices so the UI can flag them as
                # synthetic.  Real-device rows keep "collect".  An
                # operator looking at the dashboard can tell at a
                # glance "these 12 rows are fake, those 3 are real".
                source=("simulated" if (snap.vendor or "").lower() == "simulated" else "collect"),
                raw_excerpt=snap.raw_excerpt,
                errors=snap.errors or None,
                optical=list(snap.optical) if snap.optical else None,
                config_fingerprint=snap.config_fingerprint or None,
                config_lines=snap.config_lines or None,
                config_excerpt=snap.config_excerpt or None,
                metrics_run=list(snap.metrics_run) if snap.metrics_run else None,
            )
            thread_db.add(row)
            thread_db.commit()
            return snap
        except Exception as exc:  # noqa: BLE001
            logger.warning("health snapshot device {} failed: {}", device_id, exc)
            # Persist a "failed" row so the dashboard can show the gap
            # rather than silently dropping it.
            try:
                row = DeviceHealthSnapshot(
                    device_id=device_id,
                    collected_at=datetime.now(timezone.utc),
                    source="collect",
                    errors=[str(exc)[:200]],
                )
                thread_db.add(row)
                thread_db.commit()
            except Exception as inner:  # noqa: BLE001
                logger.warning("health failure-row write failed for {}: {}", device_id, inner)
            raise
        finally:
            thread_db.close()


@celery_app.task(
    name="naw.health.snapshot_one",
    bind=True,
    max_retries=1,
    time_limit=120,
    soft_time_limit=90,
)
def health_snapshot_one(self, device_id: int, metrics: list[str] | None = None) -> str:
    """Take a health snapshot for a single device. Returns 'succeeded' or 'failed'.

    ``metrics`` is an optional list of metric names to collect
    (subset of ``cpu``, ``mem``, ``temp``, ``fan``, ``power``,
    ``optical``, ``config``).  ``None``/empty means "all metrics"
    — same contract as the manual endpoint.  The list is
    forwarded to ``_health_one_device`` which then passes it to
    ``collect_health`` so the SSH command set matches the plan.
    """
    semaphore = threading.BoundedSemaphore(max(1, settings.collect_max_concurrency))
    try:
        # Normalise to a set so the filter check inside
        # ``_health_one_device`` is O(1) per metric.  We accept a
        # list at the API level for JSON ergonomics.
        metric_set = set(metrics) if metrics else None
        _health_one_device(device_id, semaphore, metrics=metric_set)
        return "succeeded"
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_snapshot_one device {} failed: {}", device_id, exc)
        return "failed"


@celery_app.task(
    name="naw.health.snapshot_all",
    bind=True,
    max_retries=1,
    rate_limit="5/m",
    time_limit=900,       # 15 min hard stop
    soft_time_limit=840,  # 14 min gentle stop
)
def health_snapshot_all(self, group_id: int | None = None) -> dict:
    """Take health snapshots for every device (or every device in a group).

    Uses the same bounded thread-pool pattern as ``batch_collect`` so a
    large fleet doesn't DDoS the SSH daemons.  Returns a summary dict
    so Celery result backend can show progress.

    Args:
        group_id: optional device-group id to limit the run.  ``None``
            means "every device in inventory".
    """
    db: Session = SessionLocal()
    try:
        q = db.query(Device)
        if group_id is not None:
            q = q.filter(Device.group_id == group_id)  # type: ignore[attr-defined]
        device_ids = [d.id for d in q.all()]

        max_conc = max(1, settings.collect_max_concurrency)
        semaphore = threading.BoundedSemaphore(max_conc)
        jitter_ms = max(0, settings.collect_jitter_ms)
        pool_workers = min(max(len(device_ids), max_conc), 32)

        succeeded = 0
        failed = 0
        # Audit the start of the run so a future "who ran a health scan
        # without using the REST endpoint?" question has an answer (e.g. a
        # future Beat schedule or a manual ``.delay()`` call). The REST
        # endpoint writes a *separate* ``health.collect`` row for the user
        # action, so the trail distinguishes "user intent" from
        # "worker activity".
        try:
            from app.services.audit import record as audit_record
            audit_record(
                db,
                actor="system",  # no per-user context inside the worker
                action="health.collect.start",
                target_kind="health",
                target_id=None,
                detail=f"mode=all devices={len(device_ids)} group_id={group_id}",
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            # Audit must not block the run.
            logger.warning("health audit (start) failed: {}", exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            futures = {
                pool.submit(_health_one_device, dev_id, semaphore, jitter_ms): dev_id
                for dev_id in device_ids
            }
            per_device_budget = settings.collect_per_device_timeout_sec + 5.0
            for future in as_completed(futures, timeout=None):
                dev_id = futures[future]
                try:
                    future.result(timeout=per_device_budget)
                    succeeded += 1
                except FuturesTimeoutError:
                    failed += 1
                    logger.warning(
                        "health_snapshot_all device {} exceeded per-device budget", dev_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        "health_snapshot_all device {} failed: {}", dev_id, exc,
                    )
        return {"total": len(device_ids), "succeeded": succeeded, "failed": failed}
    finally:
        db.close()


# ── Scheduled health sweep ───────────────────────────────────────────────
#
# Why a single periodic task and not one Celery Beat entry per plan?
# See ``app.models.scheduled_health`` docstring — Beat schedules are
# loaded at process start, but plans are CRUDed at runtime.  The
# sweep loads due plans from the DB, dispatches one
# ``health_snapshot_one`` per device per plan, and writes back
# ``last_fired_at`` / ``next_run_at`` so the next tick sees the
# updated state.
#
# One DB read per minute is fine; we deliberately don't push plans
# into Redis to avoid a second source of truth.

@celery_app.task(
    name="naw.health.scheduled_sweep",
    bind=True,
    max_retries=0,  # never auto-retry; the next tick is the recovery
    time_limit=60,
    soft_time_limit=50,
)
def health_scheduled_sweep(self) -> dict:
    """Find all plans whose ``next_run_at`` is in the past and fire them.

    Designed to be invoked by Celery Beat every 60 seconds (see
    ``beat_schedule`` in ``app.workers.celery_app``).  Returns a
    summary dict so Celery's result backend can show progress.
    """
    from app.models.scheduled_health import ScheduledHealthTask

    db: Session = SessionLocal()
    now = _utcnow()
    try:
        # ``next_run_at <= now`` is the only "due" predicate —
        # disabled plans are filtered out and re-entered only when
        # the user re-enables them (which resets ``next_run_at``).
        due = (
            db.query(ScheduledHealthTask)
            .filter(ScheduledHealthTask.enabled.is_(True))
            .filter(ScheduledHealthTask.next_run_at.isnot(None))
            .filter(ScheduledHealthTask.next_run_at <= now)
            .all()
        )

        plans_dispatched = 0
        devices_dispatched = 0
        for plan in due:
            # Resolve the effective device list at sweep time —
            # a deleted device in the plan's ``device_ids`` is
            # silently skipped, the rest still fire.
            planned = list(plan.device_ids or [])
            if planned:
                from app.models.device import Device
                existing = {
                    d.id for d in db.query(Device.id).filter(Device.id.in_(planned)).all()
                }
                target_ids = [d for d in planned if d in existing]
            else:
                # Empty ``device_ids`` → "every active device in
                # the inventory".  We resolve here so the audit
                # row tells the truth even if the plan is mutated
                # mid-flight.
                from app.models.device import Device
                target_ids = [
                    d.id for d in db.query(Device.id).filter(Device.is_active.is_(True)).all()
                ]
            if not target_ids:
                # Nothing to do — still bump next_run_at so we
                # don't re-scan the same empty plan every tick.
                plan.next_run_at = now + timedelta(minutes=plan.interval_minutes)
                plan.last_fired_at = now
                plans_dispatched += 1
                continue
            metric_set = list(plan.metrics or [])
            for dev_id in target_ids:
                if metric_set:
                    health_snapshot_one.delay(dev_id, metrics=metric_set)
                else:
                    health_snapshot_one.delay(dev_id)
                devices_dispatched += 1
            plan.last_fired_at = now
            # Don't re-fire for another ``interval_minutes``.
            plan.next_run_at = now + timedelta(minutes=plan.interval_minutes)
            plans_dispatched += 1

        if plans_dispatched:
            try:
                from app.services.audit import record as audit_record
                audit_record(
                    db,
                    actor="system",
                    action="health.schedule.sweep",
                    target_kind="health_schedule",
                    target_id=None,
                    detail=(
                        f"due={plans_dispatched} dispatched={devices_dispatched} "
                        f"now={now.isoformat()}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("health.schedule.sweep audit failed: {}", exc)
        db.commit()
        if plans_dispatched:
            logger.info(
                "health.schedule.sweep due={} dispatched={}", plans_dispatched, devices_dispatched,
            )
        return {"due": plans_dispatched, "dispatched": devices_dispatched}
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_scheduled_sweep failed: {}", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"due": 0, "dispatched": 0, "error": str(exc)[:200]}
    finally:
        db.close()


# ── Custom command collection (migrated from PythonProject5) ─────────────
#
# The original ``main.py`` / ``webapp.py`` flow let an operator paste a
# list of ``display``-style commands and have them run on every device,
# with the output persisted to ``server_config`` for later browsing.
# That workflow is preserved here as a ``Job`` of kind
# ``command_collect`` — the existing job-stream / WebSocket UI
# already understands the Job lifecycle, so the only new code is the
# per-device worker that actually does the SSH.
#
# Design:
#   * Same bounded thread-pool / semaphore / retry / jitter pattern
#     as ``batch_collect`` so a misconfigured run can't DDoS the SSH
#     daemons.
#   * Each (device, command) pair lands in the existing
#     ``config_revisions`` table (one revision per pair) with
#     ``source="command_collect"``. No new tables.
#   * Per-device and per-command errors are stored on the JobStep
#     payload so the UI can show "5/12 commands succeeded on
#     device 7" without losing the per-command detail.
#   * No approval gate — custom-command collection is read-only by
#     design (the executor rejects dangerous keywords with
#     ``__refused__`` in the output text). The mutating workflows
#     (template_apply / batch_write) keep the existing approval
#     flow.

def _command_collect_one_device(
    job_id: int,
    device_id: int,
    commands: list[str],
    initiated_by: str,
    *,
    semaphore: threading.BoundedSemaphore,
    cancel_predicate=None,
    sleep_ms: int = 0,
) -> tuple[int, str, str | None, dict]:
    """Per-device worker for the custom-command flow.

    Returns ``(device_id, status, error, per_command_results)`` where
    ``per_command_results`` is a ``{command: output}`` dict — empty
    string for skipped commands, ``__refused__:…`` for dangerous
    commands, ``__error__:…`` for transport errors.

    Each successful (device, command) pair is written as a
    ``config_revisions`` row with ``note="job#{job_id} cmd={cmd}"`` so
    the read-side API can filter back to this job.
    """
    if cancel_predicate is not None and cancel_predicate():
        return device_id, "skipped", "cancelled before start", {}

    if sleep_ms > 0:
        time.sleep(random.uniform(0, sleep_ms / 1000.0))

    with semaphore:
        # Each thread opens its own DB session + SSH session.
        # We never share a Session across threads.
        thread_db: Session = SessionLocal()
        try:
            dev, ep, cred = _resolve_endpoint(thread_db, device_id)
            try:
                _require_real_transport(dev, ep, cred, action="command-collect")
            except RuntimeError as exc:
                # Misconfigured device — surface the existing
                # remediation-hint message as the step error.
                return device_id, "failed", str(exc)[:1000], {}

            results = collect_custom_commands(
                vendor=Vendor(dev.vendor),
                address=ep.address,
                port=ep.port,
                credential=cred,
                commands=commands,
            )

            # Persist each (device, command) pair as a
            # ``config_revisions`` row so the existing
            # ``/api/v1/configs`` endpoints surface it.
            # ``__refused__`` / ``__error__`` outputs are still
            # stored, with their marker preserved — operators
            # can see "this command was refused" in the diff UI.
            for cmd, output in results.items():
                try:
                    import_text(
                        thread_db,
                        device_id=device_id,
                        content=output,
                        author=initiated_by,
                        source="command_collect",
                        note=f"job#{job_id} cmd={cmd}",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "command_collect job={} device={} cmd='{}' store failed: {}",
                        job_id, device_id, cmd[:50], exc,
                    )
            thread_db.commit()

            # Per-command error breakdown for the JobStep payload.
            refused = [c for c, o in results.items() if o.startswith("__refused__:")]
            errored = [c for c, o in results.items() if o.startswith("__error__:")]
            succeeded = len(results) - len(refused) - len(errored)
            if errored and not refused and succeeded == 0:
                status_str = "failed"
                err = f"all {len(errored)} commands errored; first: {errored[0][:64]}"
            elif refused or errored:
                status_str = "partial"
                err = (
                    f"succeeded={succeeded} refused={len(refused)} "
                    f"errored={len(errored)}"
                )
            else:
                status_str = "succeeded"
                err = None

            return device_id, status_str, err, {
                "succeeded": succeeded,
                "refused": refused,
                "errored": errored,
                "total": len(results),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "command_collect job={} device={} failed: {}",
                job_id, device_id, exc,
            )
            return device_id, "failed", str(exc)[:1000], {}
        finally:
            thread_db.close()


@celery_app.task(
    name="naw.job.command_collect",
    bind=True,
    max_retries=1,
    rate_limit="10/m",
    time_limit=1800,      # 30 min hard stop (500 devices × 50 cmds @ 5 conc)
    soft_time_limit=1680, # 28 min gentle stop
)
def command_collect(self, job_id: int) -> str:
    """Collect user-supplied commands from every device in a job.

    Uses the same bounded thread-pool / semaphore / retry / jitter
    pattern as ``batch_collect`` so a misconfigured run can't
    DDoS the SSH daemons. Returns the terminal job status
    (``succeeded`` / ``partial`` / ``failed`` / ``canceled``).
    """
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("command_collect: job {} not found", job_id)
            return "missing"

        params = job.params or {}
        commands: list[str] = list(params.get("__commands__", []) or [])
        if not commands:
            job.status = JobStatus.FAILED.value
            job.note = (job.note or "") + " | no commands in params.__commands__"
            job.finished_at = _utcnow()
            db.commit()
            return JobStatus.FAILED.value

        # Custom-command collection is read-only — skip the
        # ``requires_approval`` gate that mutating jobs go
        # through. The dangerous-command filter is the only
        # safety net.
        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or _utcnow()
        job_id_val = job.id
        initiated_by = job.initiated_by
        target_ids = list(job.target_device_ids)
        started_at = job.started_at
        db.commit()

        max_conc = max(1, settings.collect_max_concurrency)
        semaphore = threading.BoundedSemaphore(max_conc)
        jitter_ms = max(0, settings.collect_jitter_ms)

        def _cancel_predicate() -> bool:
            try:
                return _job_was_canceled(db, job)
            except Exception:  # noqa: BLE001
                return False

        pool_workers = min(max(len(target_ids), max_conc), 32)
        results: list[tuple[int, str, str | None, dict]] = []
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            futures = {
                pool.submit(
                    _command_collect_one_device,
                    job_id_val,
                    dev_id,
                    commands,
                    initiated_by,
                    semaphore=semaphore,
                    cancel_predicate=_cancel_predicate,
                    sleep_ms=jitter_ms,
                ): dev_id
                for dev_id in target_ids
            }
            # Per-device budget: commands × (read timeout + a
            # small overhead) plus the SSH connect / banner
            # drain cost.  Capped loosely so a 50-command
            # request still fits in the per-job 30-minute
            # ``time_limit`` at 5-way concurrency.
            per_command_budget = settings.ssh_read_timeout_seconds + 1.5
            per_device_budget = (
                settings.collect_per_device_timeout_sec
                + per_command_budget * len(commands)
                + 5.0
            )
            for future in as_completed(futures, timeout=None):
                dev_id = futures[future]
                try:
                    results.append(future.result(timeout=per_device_budget))
                except FuturesTimeoutError:
                    logger.warning(
                        "command_collect job {} device {} exceeded per-device budget ({}s); marking failed",
                        job_id_val, dev_id, per_device_budget,
                    )
                    results.append((dev_id, "failed", "per-device budget exceeded", {}))
                except Exception as exc:  # noqa: BLE001
                    results.append((dev_id, "failed", str(exc)[:1000], {}))

        # Reload job (cancel may have mutated it) and write
        # step records from the main thread.
        db.expire(job)
        job = db.get(Job, job_id)
        for dev_id, status, error, _detail in results:
            step = JobStep(
                job_id=job_id_val,
                device_id=dev_id,
                step_kind=JobStepKind.EXECUTE.value,
                status=status,
                error=error,
                started_at=started_at,
                finished_at=_utcnow(),
            )
            db.add(step)
        db.commit()

        # Build an aggregate summary that the UI can show
        # without re-querying the per-device config_revisions
        # rows.  ``per_command`` is a flat
        # {device_id: {succeeded, refused, errored, total}}.
        per_command: dict[int, dict] = {
            d: detail for (_d, _s, _e, detail) in results if detail
        }
        agg = {
            "total_devices": len(target_ids),
            "devices_succeeded": sum(1 for _d, s, _e, _x in results if s == "succeeded"),
            "devices_partial": sum(1 for _d, s, _e, _x in results if s == "partial"),
            "devices_failed": sum(1 for _d, s, _e, _x in results if s == "failed"),
            "devices_skipped": sum(1 for _d, s, _e, _x in results if s == "skipped"),
            "per_command": per_command,
        }
        job.summary = {**_summarize(job.steps), "command_collect": agg}
        _finalize_job_status(db, job)
        return job.status
    finally:
        db.close()
