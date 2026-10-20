"""Background scheduler for config drift detection.

This module runs **without requiring Redis** — it spawns a daemon thread
that calls ``scan_all()`` every ``INTERVAL_SECONDS``.  This means drift
detection "just works" on the SQLite dev setup (no Redis, no Celery
required).

For production deployments with Redis, set ``NAW_DRIFT_SCHEDULER_ENABLED=false``
and use Celery + Beat (``naw.drift.scan_all`` task) instead.  When both are
running they will race, so the scheduler is gated off by default in non-local
environments to prevent double-scanning.

When the in-process scheduler is enabled we additionally take a cross-process
``fcntl`` file lock so multiple uvicorn workers do not all scan at once.

Per-device failure backoff:
    When a device's ``_fetch_live_config`` call fails (SSH timeout, DNS
    error, auth failure, etc.), we record the failure and skip that
    device for ``DEVICE_BACKOFF_SECONDS``.  This prevents the scheduler
    from piling up dozens of doomed TCP-connect attempts when the
    network has a problem or a device is unreachable.  Without this
    backoff, a single misconfigured endpoint causes hundreds of wasted
    threads and fills the log with the same warning every interval —
    which made the backend *appear* to be "stuck".
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.device import Device
from app.services.drift import scan_device
from app.services.notifier import NotificationPayload, notify
from app.services.ai_monitor import analyze_drift_async


# Scan cadence is configurable via env (NAW_DRIFT_SCAN_INTERVAL_SECONDS).
# Fallback to a safe 5-minute default so drift detection "just works"
# without configuration.  Operators can tighten this (e.g. 60s) on small
# fleets or loosen it (e.g. 3600s) on large ones to reduce SSH load.
INTERVAL_SECONDS = settings.drift_scan_interval_seconds
# Backoff is intentionally short (60 s) instead of the original 15
# minutes.  Rationale: when a single SSH probe fails, it usually
# means the device is briefly unreachable (eNSP virtual-server
# restart, transient NAT timeout, etc.) rather than permanently
# misconfigured.  A 15-min backoff meant a single restart
# effectively blacked-out drift detection for the rest of the
# operator's session.  60 s keeps the failure isolation value of
# the backoff (no thundering-herd against a dead device) while
# letting drift detection recover as soon as the device does.
DEVICE_BACKOFF_SECONDS = 60
_started = False
_lock = threading.Lock()
_SCAN_LOCK_PATH = Path(tempfile.gettempdir()) / "naw-drift-scan.lock"
# Process-local backoff table: device_id → next-allowed-scan epoch.
# Cleared on restart (process state) which is fine — drift detection is
# best-effort and a missed scan after restart costs nothing.
_failures: dict[int, float] = {}
_failures_lock = threading.Lock()


@contextlib.contextmanager
def _cross_process_scan_lock():
    """Best-effort cross-process lock so only one worker scans at a time.

    Falls back to a no-op when ``fcntl`` is unavailable (Windows).
    """
    # Defer the import so Windows (which lacks fcntl) doesn't crash on startup.
    try:
        import fcntl as _fcntl  # type: ignore
    except ImportError:
        _fcntl = None  # type: ignore

    fd = None
    try:
        if _fcntl is None:
            yield True
            return

        fd = os.open(str(_SCAN_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        yield True
    except (OSError, IOError) as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            logger.debug("drift scan skipped (another worker holds the lock)")
            yield False
        else:
            yield True
    finally:
        if fd is not None:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass


def _scheduler_enabled() -> bool:
    """Return True if the in-process scheduler should start.

    Honours explicit env override first, then falls back to config ``env``:
      - NAW_DRIFT_SCHEDULER_ENABLED=true  -> force on
      - NAW_DRIFT_SCHEDULER_ENABLED=false -> force off
      - unset + settings.env == 'local'   -> on
      - unset + any other env             -> off (assumes Celery Beat handles it)
    """
    override = os.getenv("NAW_DRIFT_SCHEDULER_ENABLED", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return (settings.env or "").lower() == "local"


def _loop(stop_event: threading.Event) -> None:
    """Main loop: scan every INTERVAL_SECONDS."""
    logger.info("DriftBackgroundScheduler started (interval={}s)", INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            with _cross_process_scan_lock() as got_lock:
                if got_lock:
                    run_once()
        except Exception as exc:
            logger.exception("drift scan loop failed: {}", exc)
        # sleep in small chunks so stop_event reacts quickly
        for _ in range(INTERVAL_SECONDS * 10):
            if stop_event.is_set():
                break
            time.sleep(0.1)
    logger.info("DriftBackgroundScheduler stopped")


def run_once() -> dict:
    """One pass over every active device.  Returns the result dict."""
    db = SessionLocal()
    try:
        devices = db.query(Device).filter_by(is_active=True).all()
        now = time.time()
        results = []
        skipped_backoff = 0
        for idx, dev in enumerate(devices):
            # Per-device failure backoff: if this device failed recently,
            # skip it until the backoff expires.  Without this, a single
            # unreachable device would be probed every interval forever
            # — each probe blocks on TCP connect for ``ssh_timeout_seconds``
            # and threads pile up.
            with _failures_lock:
                next_ok = _failures.get(dev.id)
            if next_ok is not None and now < next_ok:
                skipped_backoff += 1
                continue
            # Small inter-device pause.  Some backends (notably the
            # eNSP virtual-device SSH server used in the lab) refuse
            # rapid back-to-back connections: the second connect()
            # times out before banner even though the device is up
            # and accepting the same connection a few seconds later.
            # 1.5 s is enough to let eNSP's lightweight server flush
            # its accept queue without making a 50-device fleet scan
            # feel sluggish (50 × 1.5 s = 75 s, vs 5 minutes default
            # interval — comfortably below the cadence).  We also
            # skip the sleep before the first device so a single
            # device fleet doesn't pay the cost for nothing.
            if idx > 0:
                time.sleep(1.5)
            try:
                drift = scan_device(db, dev)
                if drift:
                    results.append({
                        "device_id": dev.id,
                        "device_name": dev.name,
                        "drift_id": drift.id,
                        "severity": drift.severity,
                        "lines_added": drift.lines_added,
                        "lines_removed": drift.lines_removed,
                        "change_hint": drift.change_hint,
                    })
                    # Successful scan clears any prior failure record.
                    with _failures_lock:
                        _failures.pop(dev.id, None)
                else:
                    # scan_device returned None — could be "no drift" or
                    # "fetch failed".  We can't tell the difference here,
                    # so we don't extend the backoff (a None from
                    # scan_device is also returned when fetch fails and
                    # the warning was already logged).
                    pass
            except Exception as exc:
                logger.warning("drift scan failed device={}: {}", dev.id, exc)
                with _failures_lock:
                    _failures[dev.id] = now + DEVICE_BACKOFF_SECONDS
        db.commit()

        # Fire notifications for critical / warning events
        for r in results:
            if r["severity"] in ("critical", "warning"):
                notify(NotificationPayload(
                    title=f"配置漂移 · {r['change_hint'] or 'unknown'}",
                    body=f"设备 {r['device_name']} (id={r['device_id']}) "
                         f"偏离基准 (+{r['lines_added']} -{r['lines_removed']} 行)",
                    level=r["severity"],
                    device_id=r["device_id"],
                    device_name=r["device_name"],
                    extra={"drift_id": r["drift_id"]},
                ))
                # Trigger AI analysis asynchronously.
                analyze_drift_async(r["drift_id"], db)
        if skipped_backoff:
            logger.debug(
                "drift scan skipped {} device(s) due to backoff", skipped_backoff
            )
        return {"scanned": len(devices), "drifted": len(results), "at": datetime.now(timezone.utc).isoformat()}
    finally:
        db.close()


def start() -> None:
    """Spawn background thread (idempotent)."""
    global _started
    if not _scheduler_enabled():
        logger.info(
            "DriftBackgroundScheduler disabled by config "
            "(env={}, NAW_DRIFT_SCHEDULER_ENABLED={!r}). "
            "Use Celery Beat ('celery -A app.workers.celery_app beat') for scheduled drift scans.",
            settings.env,
            os.getenv("NAW_DRIFT_SCHEDULER_ENABLED", "<unset>"),
        )
        return
    with _lock:
        if _started:
            return
        # Stagger the very first scan by a few seconds so we don't
        # slam devices that are still coming up after the backend
        # starts (typical in dev: launch eNSP, then start the
        # backend, then open the UI; the virtual SSH server takes
        # a couple of seconds to finish its own init).  Without
        # this, the first interval always burns through 5-second
        # timeouts on every device before the operator has a
        # chance to log in.
        stop = threading.Event()
        def _delayed_loop() -> None:
            time.sleep(5)
            _loop(stop)
        t = threading.Thread(target=_delayed_loop, name="drift-scanner", daemon=True)
        t.start()
        _started = True
