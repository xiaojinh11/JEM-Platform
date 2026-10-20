"""Scheduled health collection plan CRUD.

Operators can CRUD periodic "snapshot these devices every N minutes"
plans.  The actual firing happens via the ``naw.health.scheduled_sweep``
Celery task (one task, ticking every 60s, dispatching per-plan
``snapshot_one`` calls).  See ``docs/design/COLLECTION_PATTERNS.md``
section 7 for the rationale (Beat is static; we CRUD at runtime so
we use a sweep rather than registering per-plan Beat entries).

Endpoints
---------
* ``GET    /api/v1/health/schedules``     list plans (any role)
* ``POST   /api/v1/health/schedules``     create a plan (``require_operator``)
* ``GET    /api/v1/health/schedules/{id}``  fetch one (any role)
* ``PATCH  /api/v1/health/schedules/{id}``  partial update (``require_operator``)
* ``DELETE /api/v1/health/schedules/{id}``  delete (``require_operator``)
* ``POST   /api/v1/health/schedules/{id}/run``  fire once, immediately
                                              (``require_operator``)

The list/create use ``""`` so the *outer* prefix
(``app/api/v1/__init__.py`` mounts us as ``health/schedules``) gives
the right URL.  Earlier this module used ``@router.get("/")`` and
was mounted with prefix ``health`` — that produced the path
``/health/`` which conflicted with the legacy ``/health/collect``-style
endpoints in the same namespace.

Authorization
-------------
Mirrors the manual ``/health/collect`` endpoint: ``require_operator``.
Health snapshots are read-only and idempotent; admin gating would
just be ceremony.

Audit
-----
Every create / update / delete / run-now writes an
``audit_events`` row.  The sweep itself is system-actor and writes
its own ``health.schedule.sweep`` row once per tick (best-effort,
see tasks.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.core.logging import logger
from app.db.session import get_db
from app.models.scheduled_health import ScheduledHealthTask
from app.models.user import User
from app.services.audit import record as audit_record

router = APIRouter()

# Hard floor on the interval.  Anything below this is almost
# certainly a typo (operators want minutes, not seconds) and
# would DDoS the SSH daemons if interpreted as seconds.
_MIN_INTERVAL_MINUTES = 1
# Soft cap to prevent "I want to snapshot every 1 minute forever"
# accidents.  Operators can override in the DB if they really mean
# it; the API just refuses by default.
_MAX_INTERVAL_MINUTES = 24 * 60  # one day
# Recognised metric names.  The set mirrors
# ``health_collector.DEFAULT_METRICS`` — keep in sync.
_VALID_METRICS = frozenset({"cpu", "mem", "temp", "fan", "power", "optical", "config"})


# ── Pydantic schemas ────────────────────────────────────────────────────

class ScheduledHealthTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    interval_minutes: int = Field(ge=_MIN_INTERVAL_MINUTES, le=_MAX_INTERVAL_MINUTES)
    device_ids: list[int] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: Optional[str] = Field(default=None, max_length=500)

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in _VALID_METRICS]
        if bad:
            raise ValueError(
                f"unknown metrics: {bad}; valid: {sorted(_VALID_METRICS)}"
            )
        # De-dupe while preserving order — UX is nicer when the
        # UI sends a stable order.
        seen: set[str] = set()
        out: list[str] = []
        for m in v:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @field_validator("device_ids")
    @classmethod
    def _check_device_ids(cls, v: list[int]) -> list[int]:
        if any(d <= 0 for d in v):
            raise ValueError("device_ids must be positive")
        # De-dupe to keep the sweep dispatch loop small.
        return list(dict.fromkeys(v))


class ScheduledHealthTaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    interval_minutes: Optional[int] = Field(
        default=None, ge=_MIN_INTERVAL_MINUTES, le=_MAX_INTERVAL_MINUTES,
    )
    device_ids: Optional[list[int]] = None
    metrics: Optional[list[str]] = None
    enabled: Optional[bool] = None
    description: Optional[str] = Field(default=None, max_length=500)

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        bad = [m for m in v if m not in _VALID_METRICS]
        if bad:
            raise ValueError(
                f"unknown metrics: {bad}; valid: {sorted(_VALID_METRICS)}"
            )
        return v

    @field_validator("device_ids")
    @classmethod
    def _check_device_ids(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return v
        if any(d <= 0 for d in v):
            raise ValueError("device_ids must be positive")
        return list(dict.fromkeys(v))


class ScheduledHealthTaskOut(BaseModel):
    id: int
    name: str
    interval_minutes: int
    device_ids: list[int]
    metrics: list[str]
    enabled: bool
    last_fired_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    description: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


# ── Helpers ──────────────────────────────────────────────────────────────

def _row_to_out(row: ScheduledHealthTask) -> ScheduledHealthTaskOut:
    return ScheduledHealthTaskOut(
        id=row.id,
        name=row.name or "",
        interval_minutes=row.interval_minutes,
        device_ids=list(row.device_ids or []),
        metrics=list(row.metrics or []),
        enabled=bool(row.enabled),
        last_fired_at=row.last_fired_at,
        next_run_at=row.next_run_at,
        description=row.description,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("", response_model=list[ScheduledHealthTaskOut])
def list_schedules(
    enabled: Optional[bool] = None,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ScheduledHealthTaskOut]:
    """List all plans.  Optional ``enabled`` filter for the UI."""
    q = db.query(ScheduledHealthTask).order_by(ScheduledHealthTask.id)
    if enabled is not None:
        q = q.filter(ScheduledHealthTask.enabled == enabled)
    return [_row_to_out(r) for r in q.all()]


@router.post("", response_model=ScheduledHealthTaskOut, status_code=201)
def create_schedule(
    payload: ScheduledHealthTaskCreate,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ScheduledHealthTaskOut:
    """Create a new plan.  Fires on next sweep tick."""
    now = _utcnow()
    row = ScheduledHealthTask(
        name=payload.name,
        interval_minutes=payload.interval_minutes,
        device_ids=payload.device_ids,
        metrics=payload.metrics,
        enabled=payload.enabled,
        description=payload.description,
        created_by=user.username,
        created_at=now,
        # First run is "as soon as the sweep sees it" — operators
        # expect "I just clicked create and a snapshot fired".
        next_run_at=now,
    )
    db.add(row)
    db.flush()  # populate row.id
    audit_record(
        db,
        actor=user.username,
        action="health.schedule.create",
        target_kind="health_schedule",
        target_id=str(row.id),
        detail=(
            f"name={payload.name!r} interval={payload.interval_minutes}m "
            f"devices={len(payload.device_ids)} metrics={payload.metrics}"
        ),
    )
    db.commit()
    db.refresh(row)
    logger.info(
        "health.schedule.create user={} id={} interval={}m devices={} metrics={}",
        user.username, row.id, row.interval_minutes,
        len(row.device_ids or []), row.metrics,
    )
    return _row_to_out(row)


@router.get("/{schedule_id}", response_model=ScheduledHealthTaskOut)
def get_schedule(
    schedule_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledHealthTaskOut:
    row = db.get(ScheduledHealthTask, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="schedule not found")
    return _row_to_out(row)


@router.patch("/{schedule_id}", response_model=ScheduledHealthTaskOut)
def update_schedule(
    schedule_id: int,
    payload: ScheduledHealthTaskUpdate,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ScheduledHealthTaskOut:
    """Partial update.  ``interval_minutes`` change resets
    ``next_run_at`` so a 60-minute plan that becomes a 5-minute
    plan doesn't have to wait an hour for the new cadence."""
    row = db.get(ScheduledHealthTask, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="schedule not found")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="no fields to update")
    interval_changed = (
        "interval_minutes" in data
        and data["interval_minutes"] != row.interval_minutes
    )
    for k, v in data.items():
        setattr(row, k, v)
    if interval_changed:
        # Re-base the schedule so the new cadence kicks in
        # immediately rather than after the next stale fire.
        row.next_run_at = _utcnow()
    row.updated_at = _utcnow()
    audit_record(
        db,
        actor=user.username,
        action="health.schedule.update",
        target_kind="health_schedule",
        target_id=str(row.id),
        detail=f"fields={sorted(data.keys())}",
    )
    db.commit()
    db.refresh(row)
    return _row_to_out(row)


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: int,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> None:
    row = db.get(ScheduledHealthTask, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="schedule not found")
    audit_record(
        db,
        actor=user.username,
        action="health.schedule.delete",
        target_kind="health_schedule",
        target_id=str(row.id),
        detail=f"name={row.name!r} interval={row.interval_minutes}m",
    )
    db.delete(row)
    db.commit()
    logger.info("health.schedule.delete user={} id={}", user.username, row.id)


@router.post("/{schedule_id}/run", response_model=ScheduledHealthTaskOut)
def run_schedule_now(
    schedule_id: int,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ScheduledHealthTaskOut:
    """Dispatch the plan's snapshot_one tasks immediately and reset
    its ``next_run_at`` to (now + interval).  Does NOT wait for the
    tasks to finish — the user gets back a row whose
    ``last_fired_at`` is set, and the snapshots appear in
    ``/health/snapshots`` as the workers complete.

    Why not ``.delay()`` inline?  Because we want to update
    ``last_fired_at`` *before* the tasks start, so a "what's
    currently firing?" UI panel can see "yes, the click was
    honored".  The tasks themselves fan out into the bounded
    health pool (see ``tasks.health_snapshot_one``).
    """
    from app.workers.tasks import health_snapshot_one
    from app.models.device import Device

    row = db.get(ScheduledHealthTask, schedule_id)
    if not row:
        raise HTTPException(status_code=404, detail="schedule not found")

    # Resolve the effective device list at request time.  An
    # empty ``device_ids`` means "every active device".  We
    # snapshot the resolved ids so the audit row tells the truth
    # even if the plan is mutated mid-flight.
    planned = list(row.device_ids or [])
    if planned:
        existing = {
            d.id for d in db.query(Device.id).filter(Device.id.in_(planned)).all()
        }
        target_ids = [d for d in planned if d in existing]
    else:
        target_ids = [d.id for d in db.query(Device.id).filter(Device.is_active.is_(True)).all()]

    metrics_filter = list(row.metrics or [])
    for dev_id in target_ids:
        # ``health_snapshot_one`` accepts a ``metrics`` kwarg so
        # the per-plan filter is honoured.
        if metrics_filter:
            health_snapshot_one.delay(dev_id, metrics=metrics_filter)
        else:
            health_snapshot_one.delay(dev_id)

    now = _utcnow()
    row.last_fired_at = now
    # Reset next_run_at to (now + interval) so a manually-fired
    # run doesn't immediately re-trigger on the next sweep tick.
    row.next_run_at = now + timedelta(minutes=row.interval_minutes)
    row.updated_at = now
    audit_record(
        db,
        actor=user.username,
        action="health.schedule.run",
        target_kind="health_schedule",
        target_id=str(row.id),
        detail=(
            f"manual; devices={len(target_ids)} metrics={metrics_filter} "
            f"next_run_at={row.next_run_at.isoformat()}"
        ),
    )
    db.commit()
    db.refresh(row)
    logger.info(
        "health.schedule.run user={} id={} devices={} metrics={}",
        user.username, row.id, len(target_ids), metrics_filter,
    )
    return _row_to_out(row)
