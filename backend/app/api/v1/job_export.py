"""Per-job error log export.

Why this lives in its own module instead of being squeezed into
``jobs.py``:

* It returns a streaming ``text/csv`` body rather than JSON, and the
  ``StreamingResponse`` machinery shouldn't share a router with the
  JSON-shaped ``GET /jobs/{id}`` endpoint — they have different
  exception semantics (CSV writes headers via ``Content-Disposition``
  even on 404 so the browser can name the file).

* CSV generation needs to join ``JobStep`` -> ``Device``, which the
  ``JobDetail`` schema doesn't currently carry; bolting a
  ``devices_map`` helper here keeps that load-local.

Output
------
CSV with one row per ``JobStep`` whose ``status`` is in
``{failed, needs_verification}`` OR whose ``error`` is non-null.
Columns (kept stable across schema changes — appending only):

    job_id, job_name, job_kind, job_status,
    device_id, device_name, vendor,
    step_kind, step_status, error,
    started_at, finished_at, duration_ms

The endpoint emits CSV even when there are zero rows, with only the
header — so the client always gets a parseable file.  An empty export
is therefore *not* an error: the file just says "0 errors found".
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.device import Device
from app.models.job import Job, JobStep
from app.models.user import User

router = APIRouter()

# Stable column order — append, don't reorder, so callers can write
# dashboards that key off position.
_COLUMNS: list[str] = [
    "job_id",
    "job_name",
    "job_kind",
    "job_status",
    "device_id",
    "device_name",
    "vendor",
    "step_kind",
    "step_status",
    "error",
    "started_at",
    "finished_at",
    "duration_ms",
]

# Step states that imply an operator should look at the row. Anything
# else (succeeded, pending, skipped) is intentionally excluded — the
# error log is supposed to be the *short* list of things that broke,
# not a verbose transcript.
_ERROR_STATUSES = {"failed", "needs_verification"}


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _duration_ms(start: datetime | None, end: datetime | None) -> int:
    if not start or not end:
        return 0
    # Both are tz-aware UTC; their difference is a timedelta.
    delta = end - start
    return max(0, int(delta.total_seconds() * 1000))


def _build_rows(db: Session, job: Job) -> list[dict[str, object]]:
    """Pull every step worth surfacing for this job.

    We do one extra query to bulk-load the devices referenced by the
    steps — fetching devices lazily one row at a time would N+1 the
    call.
    """
    steps: list[JobStep] = (
        db.query(JobStep)
        .filter(JobStep.job_id == job.id)
        .order_by(JobStep.id)
        .all()
    )
    if not steps:
        return []

    device_ids = {s.device_id for s in steps}
    devices: dict[int, Device] = {
        d.id: d
        for d in db.query(Device).filter(Device.id.in_(device_ids)).all()
    }

    rows: list[dict[str, object]] = []
    for s in steps:
        # Filter: step is failed / needs verification OR has an error.
        # This way a "succeeded" job with one stray warning also surfaces.
        if s.status not in _ERROR_STATUSES and not (s.error and s.error.strip()):
            continue
        d = devices.get(s.device_id)
        rows.append(
            {
                "job_id": job.id,
                "job_name": job.name,
                "job_kind": job.kind,
                "job_status": job.status,
                "device_id": s.device_id,
                "device_name": d.name if d else f"(deleted#{s.device_id})",
                "vendor": d.vendor if d else "",
                "step_kind": s.step_kind,
                "step_status": s.status,
                "error": (s.error or "").replace("\n", " ").replace("\r", " "),
                "started_at": _iso(s.started_at),
                "finished_at": _iso(s.finished_at),
                "duration_ms": _duration_ms(s.started_at, s.finished_at),
            }
        )
    return rows


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    buf = io.StringIO()
    # ``extrasaction="ignore"`` keeps new columns from breaking older
    # readers that look up an extra field.
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


@router.get("/{job_id}/errors.csv")
def export_job_errors_csv(
    job_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Download ``errors.csv`` for a single job.

    404 only when the *job* is missing. A job that exists but has no
    failed steps still returns 200 with a header-only CSV — the client
    needs to be able to *show* "this job is clean" without falling into
    an error path.
    """
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    rows = _build_rows(db, job)
    body = _csv_bytes(rows)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Filename embeds job id + name so multiple exports don't collide.
    safe_name = (job.name or f"job-{job_id}").replace("/", "-").replace("\\", "-")[:32]
    filename = f"naw-job-{job_id}-{safe_name}-errors-{stamp}.csv"

    return StreamingResponse(
        io.BytesIO(body),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
