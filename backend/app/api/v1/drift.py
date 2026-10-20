"""Config drift REST API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user, get_db, require_operator, require_operator
from app.models.drift import ConfigDrift, DriftStatus
from app.schemas.drift import (
    AcknowledgeBody,
    DriftDetail,
    DriftDeviceSummary,
    DriftListItem,
    DriftSeverityCount,
    DriftSummary,
    ScanTriggerOut,
    SetBaselineBody,
)
from app.services.drift import acknowledge_drift, set_baseline, scan_device
from app.services.notifier import NotificationPayload, notify

router = APIRouter(prefix="/drift", tags=["Drift"])


def _build_summary(db: Session) -> DriftSummary:
    total = db.query(ConfigDrift).count()
    new_count = db.query(ConfigDrift).filter_by(status=DriftStatus.NEW.value).count()
    acknowledged = db.query(ConfigDrift).filter_by(status=DriftStatus.ACKNOWLEDGED.value).count()
    critical = db.query(ConfigDrift).filter_by(severity="critical", status=DriftStatus.NEW.value).count()
    warning = db.query(ConfigDrift).filter_by(severity="warning", status=DriftStatus.NEW.value).count()
    info = db.query(ConfigDrift).filter_by(severity="info", status=DriftStatus.NEW.value).count()
    return DriftSummary(
        total=total,
        new_count=new_count,
        acknowledged=acknowledged,
        by_severity=DriftSeverityCount(critical=critical, warning=warning, info=info),
    )


@router.get("/summary", response_model=DriftSummary)
def get_summary(_user=Depends(get_current_user), db: Session = Depends(get_db)):
    return _build_summary(db)


@router.get("/devices", response_model=list[DriftDeviceSummary])
def list_devices_with_drift(
    _user=Depends(get_current_user),
    db: Session = Depends(get_db),
    severity: str | None = Query(None, description="Filter by severity: critical|warning|info"),
):
    from sqlalchemy import func
    from app.models.device import Device

    q = (
        db.query(
            Device.id.label("device_id"),
            Device.name.label("device_name"),
            Device.vendor,
            Device.group_name,
            func.max(ConfigDrift.detected_at).label("last_drift_at"),
            func.count(ConfigDrift.id).label("drift_count"),
            func.max(ConfigDrift.severity).label("latest_severity"),
        )
        .join(ConfigDrift, ConfigDrift.device_id == Device.id)
        .group_by(Device.id)
        .order_by(func.max(ConfigDrift.detected_at).desc())
    )
    if severity:
        q = q.filter(ConfigDrift.severity == severity)
    rows = q.all()
    return [
        DriftDeviceSummary(
            device_id=r.device_id,
            device_name=r.device_name,
            vendor=r.vendor,
            group_name=r.group_name,
            last_drift_at=r.last_drift_at,
            drift_count=r.drift_count,
            latest_severity=r.latest_severity,
        )
        for r in rows
    ]


@router.get("", response_model=list[DriftListItem])
def list_drifts(
    _user=Depends(get_current_user),
    db: Session = Depends(get_db),
    device_id: int | None = Query(None),
    severity: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    q = db.query(ConfigDrift).options(joinedload(ConfigDrift.device))
    if device_id:
        q = q.filter(ConfigDrift.device_id == device_id)
    if severity:
        q = q.filter(ConfigDrift.severity == severity)
    if status:
        q = q.filter(ConfigDrift.status == status)
    rows = q.order_by(ConfigDrift.detected_at.desc()).offset(offset).limit(limit).all()
    return [
        DriftListItem(
            id=d.id,
            device_id=d.device_id,
            device_name=d.device.name if d.device else "Unknown",
            vendor=d.device.vendor if d.device else "",
            group_name=d.device.group_name if d.device else None,
            detected_at=d.detected_at,
            severity=d.severity,
            status=d.status,
            lines_added=d.lines_added,
            lines_removed=d.lines_removed,
            lines_modified=d.lines_modified,
            change_hint=d.change_hint,
            acknowledged_by=d.acknowledged_by,
            acknowledged_at=d.acknowledged_at,
        )
        for d in rows
    ]


@router.get("/{drift_id}", response_model=DriftDetail)
def get_drift(drift_id: int, _user=Depends(get_current_user), db: Session = Depends(get_db)):
    d = db.query(ConfigDrift).options(joinedload(ConfigDrift.device)).get(drift_id)
    if not d:
        raise HTTPException(404, "drift not found")
    return DriftDetail(
        id=d.id,
        device_id=d.device_id,
        device_name=d.device.name if d.device else "Unknown",
        vendor=d.device.vendor if d.device else "",
        group_name=d.device.group_name if d.device else None,
        detected_at=d.detected_at,
        severity=d.severity,
        status=d.status,
        lines_added=d.lines_added,
        lines_removed=d.lines_removed,
        lines_modified=d.lines_modified,
        change_hint=d.change_hint,
        acknowledged_by=d.acknowledged_by,
        acknowledged_at=d.acknowledged_at,
        baseline_revision_id=d.baseline_revision_id,
        current_revision_id=d.current_revision_id,
        diff_snippet=d.diff_snippet,
        baseline_hash=d.baseline_hash,
        current_hash=d.current_hash,
    )


@router.post("/{drift_id}/acknowledge")
def acknowledge(
    drift_id: int,
    _: AcknowledgeBody = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    username = getattr(user, "username", "unknown")
    result = acknowledge_drift(db, drift_id, username)
    if not result:
        raise HTTPException(404, "drift not found")
    return {"ok": True, "acknowledged_by": username}


@router.post("/scan")
def trigger_scan(_user=Depends(require_operator), db: Session = Depends(get_db)):
    """Manually trigger a full drift scan. Runs synchronously (no Redis required)."""
    from app.workers.drift_scheduler import run_once
    result = run_once()
    return {"ok": True, **result}


@router.post("/scan/{device_id}")
def trigger_scan_one(device_id: int, _user=Depends(require_operator), db: Session = Depends(get_db)):
    """Manually scan a single device synchronously."""
    from app.models.device import Device
    from app.services.drift import scan_device
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "device not found")
    drift = scan_device(db, dev)
    if drift:
        return {
            "ok": True,
            "drifted": True,
            "drift_id": drift.id,
            "severity": drift.severity,
            "lines_added": drift.lines_added,
            "lines_removed": drift.lines_removed,
        }
    return {"ok": True, "drifted": False, "device_id": device_id}


@router.post("/devices/{device_id}/baseline")
def set_drift_baseline(
    device_id: int,
    body: SetBaselineBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    username = getattr(user, "username", "unknown")
    bl = set_baseline(db, device_id, body.revision_id, username)
    return {"ok": True, "device_id": device_id, "baseline_revision_id": bl.baseline_revision_id}
