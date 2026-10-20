"""Celery worker entrypoint (run via `celery -A app.workers.celery_app worker`)."""
from __future__ import annotations

from app.core.config import settings
from celery import Celery

celery_app = Celery(
    "naw",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=(
        "app.workers.tasks",
        "app.workers.document_tasks",
        "app.workers.drift_tasks",
    ),
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    # Keep broker-connect attempts short so that a misconfigured
    # deployment (no Redis, wrong port) doesn't make every API
    # call hang for 30+ seconds waiting for the broker to ack.
    # ``broker_connection_retry_on_startup`` defaults to True;
    # we set it to False so the worker (not the API) surfaces
    # the connection error early and clearly.
    broker_connection_timeout=2,
    broker_connection_retry_on_startup=False,
    # If the broker is unreachable at .delay() time, the API
    # endpoint should fail fast with a clear error rather than
    # block.  The original 30s default was too long for an
    # interactive "click to collect" UX.
    broker_transport_options={"visibility_timeout": 10},
    timezone="UTC",
    enable_utc=True,
    # ── Dev-mode fast-fail fallback ────────────────────────────────────────────
    # ``CELERY_TASK_ALWAYS_EAGER`` (env var, default "0") — when set to "1",
    # Celery executes tasks synchronously in the calling thread, bypassing
    # the broker entirely. This is invaluable in dev environments where
    # Redis isn't running. The production uvicorn invocation
    # (``start_backend.bat``) does NOT set this, so production keeps the
    # async behaviour. Tests already use pytest fixtures that override this.
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=settings.celery_task_always_eager,
    # ── Beat schedule ──────────────────────────────────────────────────────────
    # Config drift scan every 10 seconds.
    # Start the Beat with:  celery -A app.workers.celery_app beat --loglevel=INFO
    #
    # NOTE: ``naw.health.snapshot_all`` is intentionally NOT in the
    # beat_schedule. Health collection is a NEW background noise that
    # production deployments should opt into once the team is comfortable
    # with the per-device call volume. To enable, add an entry like:
    #     "health-snapshot-every-30m": {
    #         "task": "naw.health.snapshot_all",
    #         "schedule": 1800.0,
    #         "options": {"queue": "health"},
    #     }
    beat_schedule={
        "drift-scan-every-10s": {
            "task": "naw.drift.scan_all",
            "schedule": 10.0,   # seconds
            "options": {"queue": "drift"},
        },
        # ── Scheduled health sweep ───────────────────────────────────
        # One periodic task loads all due plans from the DB and
        # dispatches per-plan ``snapshot_one`` calls.  See
        # ``app.workers.tasks.health_scheduled_sweep`` for the
        # why-not-per-plan-Beat-entry rationale.
        "health-scheduled-sweep-every-60s": {
            "task": "naw.health.scheduled_sweep",
            "schedule": 60.0,
            "options": {"queue": "health"},
        },
    },
)
