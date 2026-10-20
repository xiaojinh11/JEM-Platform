"""Celery tasks for config drift detection.

Two task types:
- naw.drift.scan_one  — scans a single device (called by scan_all)
- naw.drift.scan_all  — scans all active devices; scheduled by Beat every 10 seconds
"""
from __future__ import annotations

from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.device import Device
from app.services.drift import scan_device
from app.workers import celery_app


@celery_app.task(name="naw.drift.scan_one", bind=True, max_retries=2)
def scan_one(self, device_id: int) -> dict:
    """Scan one device for config drift.  Returns dict for result backend."""
    db = SessionLocal()
    try:
        device = db.get(Device, device_id)
        if not device or not device.is_active:
            return {"device_id": device_id, "drifted": False, "reason": "inactive"}

        drift = scan_device(db, device)
        if drift:
            logger.info(
                "DRIFT detected device=%s id=%s severity=%s added=%s removed=%s",
                device.name, device_id, drift.severity,
                drift.lines_added, drift.lines_removed,
            )
            return {
                "device_id": device_id,
                "drifted": True,
                "drift_id": drift.id,
                "severity": drift.severity,
                "lines_added": drift.lines_added,
                "lines_removed": drift.lines_removed,
                "change_hint": drift.change_hint,
            }
        return {"device_id": device_id, "drifted": False}
    except Exception as exc:
        logger.warning("scan_one device=%s failed: %s", device_id, exc)
        raise self.retry(exc=exc, countdown=30)
    finally:
        db.close()


@celery_app.task(name="naw.drift.scan_all", bind=True)
def scan_all(self) -> dict:
    """Scan every active device.  Called by Celery Beat every 10 seconds."""
    db = SessionLocal()
    try:
        devices = db.query(Device).filter_by(is_active=True).all()
        results = []
        for dev in devices:
            # Run synchronously inside scan_all so we don't flood the broker.
            # Devices are scanned sequentially; parallelism can be upgraded to
            # group() later if device count > 100.
            try:
                result = scan_one.apply(
                    kwargs={"device_id": dev.id},
                )
                results.append(result.get())
            except Exception as exc:
                logger.warning("scan_one device=%s raised: %s", dev.id, exc)
                results.append({"device_id": dev.id, "drifted": False, "error": str(exc)})

        drifted = [r for r in results if r.get("drifted")]
        return {
            "total": len(devices),
            "scanned": len(results),
            "drifted": len(drifted),
            "critical": sum(1 for r in drifted if r.get("severity") == "critical"),
            "devices": drifted,
        }
    finally:
        db.close()
