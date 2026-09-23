"""Celery tasks for batch read/write/backup."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobStepKind, Vendor
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.device import Device, DeviceEndpoint
from app.models.credential import Credential
from app.models.job import Job, JobStep, BackupSnapshot
from app.services.executor import collect_config, apply_config, ExecutorError
from app.services.configs import store_snapshot, import_text
from app.services.templates import render, extract_commands
from app.workers import celery_app

# Concurrency limits for SSH operations
MAX_SSH_CONCURRENCY = 5  # Limit simultaneous SSH connections to prevent resource exhaustion


def _utcnow() -> datetime:
    """Return current UTC time with timezone info (Python 3.12+ safe)."""
    return datetime.now(timezone.utc)


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


# ── Thread-safe worker (no shared SQLAlchemy Session) ────────────────────────
def _collect_one_device_thread(job_id: int, device_id: int, initiated_by: str) -> tuple[int, str, str | None]:
    """Per-device worker. Each call gets its own DB session. Returns (device_id, status, error)."""
    thread_db: Session = SessionLocal()
    try:
        dev, ep, cred = _resolve_endpoint(thread_db, device_id)
        if dev.is_simulated or not ep or not cred:
            text = (
                "!\nsysname simulated\n"
                f"hostname simulated-{dev.name}\n"
                "return\n"
            )
        else:
            text = collect_config(
                vendor=Vendor(dev.vendor),
                address=ep.address,
                port=ep.port,
                credential=cred,
            )
        import_text(
            thread_db,
            device_id=device_id,
            content=text,
            author=initiated_by,
            source="collect",
            note=f"job#{job_id}",
        )
        return device_id, "succeeded", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("collect job {} device {} failed: {}", job_id, device_id, exc)
        return device_id, "failed", str(exc)[:1000]
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
)
def batch_collect(self, job_id: int) -> str:
    """Collect config from all devices in a job using a thread pool.

    Workers are fully isolated: each thread opens its own DB session.
    Step records and final status are written by the main (Celery) thread.
    """
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return "missing"

        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or _utcnow()
        # Snapshot primitives before threads start; never pass the Session to threads.
        job_id_val = job.id
        initiated_by = job.initiated_by
        target_ids = list(job.target_device_ids)
        started_at = job.started_at
        db.commit()

        # Thread pool: each worker manages its own SessionLocal().
        results: list[tuple[int, str, str | None]] = []
        with ThreadPoolExecutor(max_workers=MAX_SSH_CONCURRENCY) as pool:
            futures = {
                pool.submit(_collect_one_device_thread, job_id_val, dev_id, initiated_by): dev_id
                for dev_id in target_ids
            }
            for future in as_completed(futures):
                dev_id = futures[future]
                try:
                    results.append(future.result())
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
        if job.summary["failed"] == 0:
            job.status = JobStatus.SUCCEEDED.value
        elif job.summary["succeeded"] == 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.PARTIAL.value
        job.finished_at = _utcnow()
        db.commit()
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
)
def batch_apply(self, job_id: int, template_text: str, params_json: dict) -> str:
    """Render template per device, run precheck, backup, apply, verify."""
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return "missing"
        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or _utcnow()
        db.commit()

        for device_id in job.target_device_ids:
            dev, ep, cred = _resolve_endpoint(db, device_id)
            commands: list[str] = []
            try:
                rendered = render(template_text, **(params_json or {}))
                commands = extract_commands(rendered)
            except Exception as exc:  # noqa: BLE001
                _record_step(db, job, device_id, JobStepKind.PRECHECK.value, "failed", error=str(exc))
                db.commit()
                continue

            backup_step = _record_step(db, job, device_id, JobStepKind.BACKUP_BEFORE.value, "running")
            db.commit()
            try:
                if dev.is_simulated or not ep or not cred:
                    cfg = (
                        "!\nsysname simulated\n"
                        f"hostname simulated-{dev.name}\nreturn\n"
                    )
                else:
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
            try:
                if dev.is_simulated or not ep or not cred:
                    log = "\n".join([f"apply: {c}" for c in commands])
                else:
                    log = apply_config(
                        vendor=Vendor(dev.vendor),
                        address=ep.address,
                        port=ep.port,
                        credential=cred,
                        commands=commands,
                    )
                apply_step.payload = {"log_preview": log[:2000]}
                apply_step.status = "succeeded" if not dev.is_simulated else "needs_verification"
            except Exception as exc:  # noqa: BLE001
                apply_step.status = "failed"
                apply_step.error = str(exc)[:1000]
            apply_step.finished_at = _utcnow()
            db.commit()

            if apply_step.status == "succeeded":
                after_step = _record_step(db, job, device_id, JobStepKind.BACKUP_AFTER.value, "running")
                db.commit()
                try:
                    if dev.is_simulated or not ep or not cred:
                        cfg_after = "!\nafter\nreturn\n"
                    else:
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

        job.summary = _summarize(job.steps)
        if job.summary["failed"] == 0 and job.summary["needs_verification"] == 0:
            job.status = JobStatus.SUCCEEDED.value
        elif job.summary["succeeded"] == 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.NEEDS_VERIFICATION.value if job.summary["needs_verification"] else JobStatus.PARTIAL.value
        job.finished_at = _utcnow()
        db.commit()

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


def _record_step(db: Session, job: Job, device_id: int, kind: str, status: str, *, error: str | None = None) -> JobStep:
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
    return step
