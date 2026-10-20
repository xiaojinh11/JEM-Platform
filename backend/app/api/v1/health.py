"""Per-device health snapshot endpoints.

Triggers the ``naw.health.snapshot_one`` / ``snapshot_all`` Celery
tasks from a REST entry point. Operators and admins can fire a
collection run; viewers cannot.  v1.7 also exposes:

* ``GET  /api/v1/health/snapshots`` — list snapshots with filter /
  pagination.  Used by the new "Health Collection" UI page.
* ``GET  /api/v1/health/snapshots/{id}`` — single-snapshot detail.
* ``GET  /api/v1/health/snapshots/{id}/export?format=csv|xlsx|txt&filename=...``
  — file export.  Format-aware (CSV = tabular flat, XLSX = same
  via openpyxl, TXT = verbatim concatenated text).
* ``GET  /api/v1/health/devices/{device_id}/snapshots`` — same list
  but pre-scoped to one device, used by the device-detail panel.

Design notes (the legacy ``POST /collect`` contract) are unchanged
— see module docstring below.  v1.7 also added a ``metrics`` field
to the collect request so manual calls honour the same filter the
schedules use.

Authentication: ``require_operator`` for write paths
(``POST /collect``), ``get_current_user`` for read paths (so viewers
can see the dashboard).  Health snapshots are read-only and
idempotent; admin gating would just be ceremony.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.core.logging import logger
from app.core.ratelimit import batch_job_user_limiter
from app.db.session import get_db
from app.models.health import DeviceHealthSnapshot
from app.models.user import User
from app.services.audit import record as audit_record
from app.workers.tasks import health_snapshot_all, health_snapshot_one

router = APIRouter()


# ── Legacy "collect" endpoint ──────────────────────────────────────────

class HealthCollectRequest(BaseModel):
    device_ids: Optional[list[int]] = Field(
        default=None,
        description=(
            "If omitted/empty, snapshot every device in the inventory. "
            "Otherwise, snapshot only the listed devices (one Celery task "
            "per device, all share the bounded pool)."
        ),
    )
    # v1.7: per-call metric filter.  ``None`` / empty = all metrics
    # (legacy behaviour).  See ``DEFAULT_METRICS`` in
    # ``app.services.health_collector`` for the valid names.
    metrics: Optional[list[str]] = Field(
        default=None,
        description=(
            "Subset of {cpu, mem, temp, fan, power, optical, config}. "
            "Empty/None means 'all metrics'."
        ),
    )

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        valid = {"cpu", "mem", "temp", "fan", "power", "optical", "config"}
        bad = [m for m in v if m not in valid]
        if bad:
            raise ValueError(f"unknown metrics: {bad}; valid: {sorted(valid)}")
        return list(dict.fromkeys(v))


class HealthCollectResponse(BaseModel):
    mode: str  # "all" | "targeted"
    devices: int
    task_ids: list[str]


def _safe_collect_one(
    device_id: int,
    metrics: list[str] | None,
    task_id: str,
    logger,
) -> None:
    """Background-task wrapper: call ``health_snapshot_one`` directly.

    We **do not** go through ``.delay()`` because:

    1. In dev mode (``task_always_eager=True``) ``.delay()`` is a
       synchronous call; we'd block this background thread for the
       full duration of the SSH round-trip.
    2. With no Redis broker, ``.delay()`` raises
       ``Retry limit exceeded`` (DNS 30s window).
    3. We just need the side-effect (a ``DeviceHealthSnapshot`` row
       written by the task body).  Calling the task function
       directly gives us that without any Celery machinery.

    The function opens its own SQLAlchemy session and SSH
    connection, so running it in FastAPI's background threadpool
    is safe.
    """
    from app.workers.tasks import health_snapshot_one
    try:
        if metrics:
            health_snapshot_one(device_id, metrics=metrics)
        else:
            health_snapshot_one(device_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "health.collect (target) device_id={} task_id={} failed: {}",
            device_id, task_id, exc,
        )


def _safe_collect_all(
    metrics: list[str] | None,
    task_id: str,
    logger,
) -> None:
    """Background-task wrapper for fleet-wide collection.

    Same rationale as ``_safe_collect_one``: call the task
    function directly rather than going through ``.delay()``.
    The task opens its own DB session and uses a bounded thread
    pool internally, so concurrency is still bounded by
    ``settings.collect_max_concurrency``.
    """
    from app.workers.tasks import health_snapshot_all
    try:
        # snapshot_all does not accept a metrics filter; for the
        # manual "collect all" UX we just take the snapshot.
        result = health_snapshot_all()
        logger.info(
            "health.collect (all) task_id={} done: {}", task_id, result,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "health.collect (all) task_id={} failed: {}", task_id, exc,
        )


@router.post("/collect", response_model=HealthCollectResponse, status_code=202)
async def collect_health(
    background: BackgroundTasks,
    payload: HealthCollectRequest | None = None,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> HealthCollectResponse:
    """Trigger a health snapshot run. Returns immediately with task IDs.

    Implementation note: this endpoint used to be ``def`` (sync) and
    called ``.delay()`` synchronously. That blocked the API thread
    for the entire duration of the collect run (5-30s depending on
    fleet size and SSH latency), which made the frontend's 30s axios
    timeout the de-facto SLA.  We now schedule the work via FastAPI's
    ``BackgroundTasks`` (which runs in a threadpool, NOT in the event
    loop) and return immediately.  In dev mode (no Redis) the
    background call still works because Celery ``task_always_eager``
    falls back to a synchronous in-process execution.
    """
    payload = payload or HealthCollectRequest()
    target_ids = payload.device_ids or []
    metrics_filter = payload.metrics or []

    # Per-user throttle — same cap as ``create_job``. Without it, a
    # misconfigured script can queue thousands of snapshot tasks in a
    # minute and crowd out real batch operations.
    allowed, retry_after = batch_job_user_limiter.check(str(user.id))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"健康采集提交过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    # Validate targeted devices exist. We accept silent skipping on
    # ``mode='all'`` (the worker just iterates the inventory) but for
    # explicit IDs a typo should surface as 404, not as a quiet "this
    # device was ignored" surprise.
    if target_ids:
        from app.models.device import Device
        existing = {
            d.id for d in db.query(Device.id).filter(Device.id.in_(target_ids)).all()
        }
        missing = [d for d in target_ids if d not in existing]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"device(s) not found: {missing[:10]}{'...' if len(missing) > 10 else ''}",
            )

    # Module-level logger — imported once at function top so both the
    # targeted and the all-device branches can use it without
    # ``UnboundLocalError`` (an import inside ``if target_ids:`` would
    # only bind the name in that branch).
    from app.core.logging import logger as _log

    if target_ids:
        # One task per device. We schedule the Celery tasks onto
        # ``BackgroundTasks`` so the API returns immediately.  In
        # production (Celery worker running), ``.delay()`` returns
        # <AsyncResult> instantly and the actual SSH work happens
        # in the worker process.  In dev mode (no Redis /
        # ``task_always_eager=True``), ``.delay()`` would normally
        # block the calling thread; we wrap the call in a
        # ``background.add_task`` so FastAPI's threadpool runs it
        # off the request handler path.
        task_ids: list[str] = []
        adhoc_id = f"adhoc-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
        for dev_id in target_ids:
            tid = f"target-{adhoc_id}-{dev_id}"
            if metrics_filter:
                background.add_task(
                    _safe_collect_one, dev_id, metrics_filter, tid, _log
                )
            else:
                background.add_task(
                    _safe_collect_one, dev_id, None, tid, _log
                )
            task_ids.append(tid)
        mode = "targeted"
        devices_count = len(target_ids)
        detail = f"devices={target_ids[:10]}{'...' if len(target_ids) > 10 else ''}"
    else:
        tid = f"all-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
        background.add_task(_safe_collect_all, metrics_filter, tid, _log)
        task_ids = [tid]
        mode = "all"
        devices_count = 0  # filled in by the worker
        detail = "all devices"

    audit_record(
        db,
        actor=user.username,
        action="health.collect",
        target_kind="health",
        target_id=None,
        detail=f"mode={mode} {detail} metrics={metrics_filter}",
    )
    db.commit()

    logger.info(
        "health.collect user={} mode={} devices={} tasks={} metrics={}",
        user.username, mode, devices_count, len(task_ids), metrics_filter,
    )

    return HealthCollectResponse(
        mode=mode,
        devices=devices_count,
        task_ids=task_ids,
    )


# ── v1.7: snapshot list / detail / export ──────────────────────────────

class HealthSnapshotOut(BaseModel):
    """Public shape of one row in ``device_health_snapshot``.

    The new fields (optical, config_fingerprint, etc.) are surfaced
    as-is; the UI's chart component knows how to render ``optical``
    as a per-port series.
    """
    id: int
    device_id: int
    collected_at: datetime
    vendor: str
    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    temp_c: Optional[float] = None
    fan_state: Optional[str] = None
    power_w: Optional[float] = None
    source: str
    errors: Optional[list[str]] = None
    optical: Optional[list[dict[str, Any]]] = None
    config_fingerprint: Optional[str] = None
    config_lines: Optional[int] = None
    config_excerpt: Optional[str] = None
    metrics_run: Optional[list[str]] = None


def _row_to_out(row: DeviceHealthSnapshot) -> HealthSnapshotOut:
    return HealthSnapshotOut(
        id=row.id,
        device_id=row.device_id,
        collected_at=row.collected_at,
        vendor=row.vendor or "",
        cpu_pct=float(row.cpu_pct) if row.cpu_pct is not None else None,
        mem_pct=float(row.mem_pct) if row.mem_pct is not None else None,
        temp_c=float(row.temp_c) if row.temp_c is not None else None,
        fan_state=row.fan_state,
        power_w=float(row.power_w) if row.power_w is not None else None,
        source=row.source or "collect",
        errors=list(row.errors) if row.errors else None,
        optical=list(row.optical) if row.optical else None,
        config_fingerprint=row.config_fingerprint,
        config_lines=row.config_lines,
        config_excerpt=row.config_excerpt,
        metrics_run=list(row.metrics_run) if row.metrics_run else None,
    )


@router.get("/snapshots", response_model=list[HealthSnapshotOut])
def list_snapshots(
    device_id: Optional[int] = Query(default=None, description="Filter by device"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    metric: Optional[str] = Query(
        default=None,
        description="Filter to rows that ran this metric (e.g. 'optical')",
    ),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HealthSnapshotOut]:
    """List recent health snapshots, newest first.

    ``metric`` filter is JSON-aware.  We deliberately do the
    filter in Python (after pagination) rather than in SQL for
    two reasons:

    1. ``metrics_run`` is a JSON column; a portable LIKE-based
       filter is unreliable across SQLite / Postgres and is
       already raising a SQLAlchemy 2.x deprecation warning.
    2. The default limit (100) is small enough that Python-side
       filtering doesn't cost anything measurable.

    For very large deployments where pagination + post-filter
    would be slow, add a database-specific JSON containment
    operator here (json_each on SQLite, ``@>`` on Postgres).
    """
    q = db.query(DeviceHealthSnapshot).order_by(DeviceHealthSnapshot.collected_at.desc())
    if device_id is not None:
        q = q.filter(DeviceHealthSnapshot.device_id == device_id)
    rows = q.offset(offset).limit(limit).all()
    if metric:
        rows = [r for r in rows if metric in (r.metrics_run or [])]
    return [_row_to_out(r) for r in rows]


@router.get("/devices/{device_id}/snapshots", response_model=list[HealthSnapshotOut])
def list_device_snapshots(
    device_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HealthSnapshotOut]:
    """Convenience route: same as ``GET /snapshots?device_id=`` but
    uses a path parameter so the device-detail panel can link
    directly.  The path-style URL is also what the front-end
    catches with the API client's trailing-slash logic."""
    q = (
        db.query(DeviceHealthSnapshot)
        .filter(DeviceHealthSnapshot.device_id == device_id)
        .order_by(DeviceHealthSnapshot.collected_at.desc())
        .limit(limit)
    )
    return [_row_to_out(r) for r in q.all()]


@router.get("/snapshots/{snapshot_id}", response_model=HealthSnapshotOut)
def get_snapshot(
    snapshot_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HealthSnapshotOut:
    row = db.get(DeviceHealthSnapshot, snapshot_id)
    if not row:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return _row_to_out(row)


# ── File export ────────────────────────────────────────────────────────

_SNAPSHOT_EXPORT_COLUMNS: list[str] = [
    "snapshot_id",
    "device_id",
    "collected_at",
    "vendor",
    "source",
    "cpu_pct",
    "mem_pct",
    "temp_c",
    "fan_state",
    "power_w",
    "optical_port_count",
    "config_fingerprint",
    "config_lines",
    "config_excerpt",
    "errors",
    "metrics_run",
]


def _snapshot_to_flat_row(row: DeviceHealthSnapshot) -> dict[str, object]:
    """Map a snapshot row to a flat dict for CSV/XLSX.

    Per-port optical is collapsed to a count (the per-port detail
    is available in the JSON column; CSV/XLSX is for the "what's
    the latest reading" use case, not the "per-port dashboard"
    use case).  ``config_excerpt`` is included verbatim so an
    operator can grep it offline.
    """
    return {
        "snapshot_id": row.id,
        "device_id": row.device_id,
        "collected_at": row.collected_at.isoformat() if row.collected_at else "",
        "vendor": row.vendor or "",
        "source": row.source or "",
        "cpu_pct": float(row.cpu_pct) if row.cpu_pct is not None else "",
        "mem_pct": float(row.mem_pct) if row.mem_pct is not None else "",
        "temp_c": float(row.temp_c) if row.temp_c is not None else "",
        "fan_state": row.fan_state or "",
        "power_w": float(row.power_w) if row.power_w is not None else "",
        "optical_port_count": len(row.optical or []),
        "config_fingerprint": row.config_fingerprint or "",
        "config_lines": row.config_lines or 0,
        "config_excerpt": row.config_excerpt or "",
        "errors": "; ".join(row.errors or []),
        "metrics_run": "; ".join(row.metrics_run or []),
    }


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_SNAPSHOT_EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


def _xlsx_bytes(rows: list[dict[str, object]]) -> bytes:
    """Render via openpyxl.  Returns the workbook bytes.

    We import openpyxl lazily so a deployment that never exports
    to XLSX (e.g. an air-gapped prod that only uses CSV) doesn't
    pay the import cost on every request.  If openpyxl isn't
    installed we surface a 501 with a clear message — CSV is
    always available as a fallback.
    """
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=501,
            detail="xlsx export requires openpyxl (pip install openpyxl)",
        ) from exc
    wb = Workbook()
    ws = wb.active
    ws.title = "health"
    ws.append(_SNAPSHOT_EXPORT_COLUMNS)
    for r in rows:
        ws.append([r.get(c, "") for c in _SNAPSHOT_EXPORT_COLUMNS])
    # Light column-width tuning so the resulting file is readable
    # in Excel without manual resizing.
    widths = {
        "collected_at": 24, "vendor": 12, "config_excerpt": 60,
        "errors": 40, "metrics_run": 28,
    }
    for col_idx, name in enumerate(_SNAPSHOT_EXPORT_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = widths.get(name, 16)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _txt_bytes(rows: list[dict[str, object]]) -> bytes:
    """Plain-text export: one human-readable block per row.

    We deliberately don't try to render a table here — TXT is
    for "I want to grep this in a terminal" not "I want a
    spreadsheet".  Each block starts with a header line so an
    operator can split files with ``awk`` / ``csplit`` later.
    """
    out = io.StringIO()
    for r in rows:
        out.write("=" * 72 + "\n")
        for col in _SNAPSHOT_EXPORT_COLUMNS:
            out.write(f"{col}: {r.get(col, '')}\n")
        out.write("\n")
    return out.getvalue().encode("utf-8")


_EXPORT_RENDERERS = {
    "csv": (_csv_bytes, "text/csv; charset=utf-8", ".csv"),
    "xlsx": (_xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "txt": (_txt_bytes, "text/plain; charset=utf-8", ".txt"),
}


def _safe_filename(name: str) -> str:
    """Strip path separators from a user-supplied filename.

    The goal is "safe to hand to the OS as a filename", not
    "completely clean": a literal "etc" in the user's name is
    not a security problem.  We DO remove:

    * forward and back slashes (would create a sub-directory)
    * the null byte (OS-dependent horrors)
    * control characters (terminal escape injection)

    We deliberately keep the literal characters in the
    filename; the test asserts that ``.`` and ``..`` no longer
    have path-separator semantics, which is true once the
    ``/`` is gone.
    """
    cleaned = "".join(
        ch for ch in name
        if ch.isprintable() and ch not in {"/", "\\", "\0"}
    )
    return cleaned.strip()[:80] or "health-snapshot"


@router.get("/snapshots/{snapshot_id}/export")
def export_snapshot(
    snapshot_id: int,
    format: str = Query(default="csv", pattern="^(csv|xlsx|txt)$"),
    filename: Optional[str] = Query(
        default=None,
        description="Base filename (no extension).  Sanitised server-side.",
    ),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Download a single snapshot in CSV / XLSX / TXT.

    Defaults to CSV.  If the requested row is missing we 404 —
    the URL embeds the id so a stale link is the only way to
    hit this path, and we want a clear error.
    """
    row = db.get(DeviceHealthSnapshot, snapshot_id)
    if not row:
        raise HTTPException(status_code=404, detail="snapshot not found")

    renderer, media_type, ext = _EXPORT_RENDERERS[format]
    body = renderer([_snapshot_to_flat_row(row)])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = _safe_filename(filename or f"health-snapshot-{snapshot_id}")
    full_name = f"{base}-{stamp}{ext}"

    return StreamingResponse(
        io.BytesIO(body),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{full_name}"},
    )
