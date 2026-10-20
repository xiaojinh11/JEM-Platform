"""Config drift detection service."""
from __future__ import annotations

import difflib
import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.enums import Vendor
from app.core.logging import logger
from app.models.drift import ConfigDrift, DriftBaseline, DriftSeverity, DriftStatus
from app.models.device import Device, DeviceEndpoint
from app.models.credential import Credential
from app.models.config import ConfigArchive, ConfigRevision
from app.services.executor import collect_config
from app.services.configs import read_revision_text


def _compute_diff(baseline_text: str, current_text: str, max_lines: int = 40) -> tuple[int, int, int, str]:
    """Return (added, removed, modified, snippet) from unified diff."""
    a_lines = baseline_text.splitlines(keepends=True)
    b_lines = current_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(a_lines, b_lines, n=1))
    # Count
    adds = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    snippet = "".join(diff[:max_lines])
    return adds, dels, 0, snippet


def _heuristic_change_hint(current_text: str) -> Optional[str]:
    """Very naive attempt to guess who/what changed the config."""
    lines = [l.strip() for l in current_text.splitlines() if l.strip() and not l.startswith("!")]
    for line in lines[:10]:
        lower = line.lower()
        if "nmp" in lower and "community" in lower:
            return "snmp-config"
        if "ntp-server" in lower or "ntp unicast-server" in lower:
            return "ntp-config"
        if "acl" in lower and ("rule" in lower or "number" in lower):
            return "acl-change"
        if "interface" in lower and "ip address" in lower:
            return "ip-change"
        if "sysname" in lower:
            return "hostname-change"
    return None


def _compute_severity(lines_added: int, lines_removed: int) -> DriftSeverity:
    total = lines_added + lines_removed
    if total >= 20:
        return DriftSeverity.CRITICAL
    if total >= 5:
        return DriftSeverity.WARNING
    return DriftSeverity.INFO


def _fetch_live_config(db: Session, device: Device) -> Optional[str]:
    """SSH to device and return its running config, or None on failure."""
    ep = (
        db.query(DeviceEndpoint)
        .filter_by(device_id=device.id, is_primary=True)
        .first()
    )
    if not ep:
        ep = db.query(DeviceEndpoint).filter_by(device_id=device.id).first()
    if not ep:
        return None
    cred = db.get(Credential, ep.credential_id) if ep.credential_id else None
    if not cred:
        return None
    # eNSP's virtual SSH server is famous for occasionally accepting
    # the TCP connection but failing to send the banner within the
    # ``banner_timeout`` window.  A single retry after a short
    # pause is overwhelmingly enough to recover — the second
    # connect() either gets a fresh accept or sees the server
    # recover.  Without the retry, a single bad boot of eNSP
    # blacklists the device in drift detection for the whole
    # backoff window.
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            if device.is_simulated:
                return "!\nsysname simulated\nhostname simulated-{}\nreturn\n".format(device.name)
            # Drift scans cap the SSH connect at 5s so a single dead host
            # can't block the scheduler loop.  The full ``collect_config``
            # still honours ``settings.ssh_timeout_seconds`` for user-driven
            # jobs (batch collection, WebSSH handshake).
            return collect_config(
                vendor=Vendor(device.vendor),
                address=ep.address,
                port=ep.port,
                credential=cred,
                timeout_override=5,
            )
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                # Brief pause before retry — eNSP's banner bug
                # typically clears within a second or two.
                import time as _t
                _t.sleep(1.5)
    logger.warning("drift fetch config failed for device {}: {}", device.id, last_exc)
    # Mark the device as failing so the scheduler's per-device
    # backoff skips it for DEVICE_BACKOFF_SECONDS.  Without this,
    # the scheduler would re-attempt every interval and block on
    # the same TCP-connect timeout each time.  Match the
    # scheduler's shorter 60-s backoff so a transient eNSP
    # restart doesn't black-out drift detection for the rest of
    # the session.
    try:
        from app.workers.drift_scheduler import _failures, _failures_lock
        import time as _t
        with _failures_lock:
            _failures[device.id] = _t.time() + 60
    except Exception:
        pass
    return None


def scan_device(db: Session, device: Device) -> Optional[ConfigDrift]:
    """Check one device for config drift.

    Logic:
    1. If a DriftBaseline exists → compare against that revision's text.
    2. Else → compare against the latest ConfigRevision (if any).
    3. Else → no baseline → record device's live config as baseline (no drift event).

    Returns a new ConfigDrift row if drift is detected, otherwise None.
    """
    # Resolve baseline text
    baseline_text: Optional[str] = None
    baseline_rev_id: Optional[int] = None

    bl = device.drift_baseline
    if bl and bl.baseline_revision_id:
        rev = db.get(ConfigRevision, bl.baseline_revision_id)
        if rev:
            baseline_text = read_revision_text(rev)
            baseline_rev_id = rev.id
    else:
        # Fall back to latest revision
        arc = db.query(ConfigArchive).filter_by(device_id=device.id).first()
        if arc and arc.latest_revision_id:
            rev = db.get(ConfigRevision, arc.latest_revision_id)
            if rev:
                baseline_text = read_revision_text(rev)
                baseline_rev_id = rev.id

    # No baseline at all → first time, record live config as baseline
    if baseline_text is None:
        live = _fetch_live_config(db, device)
        if live is None:
            return None  # Can't reach device
        # Store a dummy baseline hash so next time we compare
        # We record a sentinel drift with severity=info to initialise tracking
        drift = ConfigDrift(
            device_id=device.id,
            baseline_revision_id=None,
            current_revision_id=None,
            severity=DriftSeverity.INFO.value,
            status=DriftStatus.NEW.value,
            lines_added=0,
            lines_removed=0,
            lines_modified=0,
            baseline_hash=None,
            current_hash=None,
            diff_snippet=None,
            change_hint="initial-baseline",
        )
        db.add(drift)
        db.commit()
        db.refresh(drift)
        return drift

    # Fetch current live config
    live = _fetch_live_config(db, device)
    if live is None:
        return None

    # Hash comparison (fast path)
    def _h(t: str) -> str:
        return hashlib.sha256(t.encode()).hexdigest()

    if _h(baseline_text) == _h(live):
        return None  # No change

    # Compute detailed diff
    added, removed, modified, snippet = _compute_diff(baseline_text, live)
    severity = _compute_severity(added, removed)
    hint = _heuristic_change_hint(live)

    # Find current revision (latest)
    arc = db.query(ConfigArchive).filter_by(device_id=device.id).first()
    current_rev_id: Optional[int] = None
    if arc and arc.latest_revision_id:
        current_rev_id = arc.latest_revision_id

    drift = ConfigDrift(
        device_id=device.id,
        baseline_revision_id=baseline_rev_id,
        current_revision_id=current_rev_id,
        severity=severity.value,
        status=DriftStatus.NEW.value,
        lines_added=added,
        lines_removed=removed,
        lines_modified=modified,
        baseline_hash=_h(baseline_text),
        current_hash=_h(live),
        diff_snippet=snippet,
        change_hint=hint,
    )
    db.add(drift)
    db.commit()
    db.refresh(drift)
    return drift


def acknowledge_drift(db: Session, drift_id: int, username: str) -> Optional[ConfigDrift]:
    d = db.get(ConfigDrift, drift_id)
    if not d:
        return None
    d.status = DriftStatus.ACKNOWLEDGED.value
    d.acknowledged_by = username
    d.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(d)
    return d


def set_baseline(db: Session, device_id: int, revision_id: int | None, username: str) -> DriftBaseline:
    """Set (or clear) the gold baseline revision for a device."""
    bl = db.query(DriftBaseline).filter_by(device_id=device_id).first()
    if bl:
        bl.baseline_revision_id = revision_id
        bl.created_by = username
        bl.created_at = datetime.now(timezone.utc)
    else:
        bl = DriftBaseline(
            device_id=device_id,
            baseline_revision_id=revision_id,
            created_by=username,
        )
        db.add(bl)
    db.commit()
    db.refresh(bl)
    return bl
