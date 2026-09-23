"""Bulk export API — download all data as JSON or CSV.

Exports are always filtered by the caller's session, but since this app
is single-tenant (no org isolation), every logged-in user can export
everything.  The output intentionally omits secrets (plain-text passwords,
SSH keys) so exports are safe to share.

Query parameters
---------------
``format``: ``json`` (default) | ``csv``

``scope``: ``devices`` | ``credentials`` | ``configs`` | ``documents`` |
           ``drift`` | ``audit`` | ``all``

``since``: ISO-8601 datetime string. If given, only records updated
           at or after this timestamp are returned. Useful for incremental
           backups (``rsync``-style diffs).

Output
------
JSON — a top-level object with one key per scope::

    {"devices": [...], "credentials": [...], "exported_at": "..."}

CSV — a ZIP archive containing one ``.csv`` file per scope, so that
data with different column sets never has to share a header row.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _dt() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_csv(rows: list[dict[str, Any]], filename: str) -> bytes:
    buf = io.StringIO()
    if not rows:
        writer = csv.writer(buf)
        writer.writerow([])
        return buf.getvalue().encode()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


# ─────────────────────────────────────────────────────────────────────
# Export scopes
# ─────────────────────────────────────────────────────────────────────

def _devices(db: Session, since: str | None) -> list[dict[str, Any]]:
    from app.models.device import Device, DeviceEndpoint
    q = db.query(Device).order_by(Device.id)
    if since:
        q = q.filter(Device.updated_at >= since)
    rows = []
    for d in q.all():
        eps = db.query(DeviceEndpoint).filter_by(device_id=d.id, is_primary=True).all()
        ip = eps[0].address if eps else ""
        cred_id = eps[0].credential_id if eps else None
        rows.append({
            "id": d.id,
            "name": d.name,
            "hostname": d.hostname or "",
            "vendor": d.vendor,
            "model": d.model or "",
            "software_version": d.software_version or "",
            "role": d.role or "",
            "status": "active" if d.is_active else "inactive",
            "location": d.location or "",
            "tags": d.tags or "",
            "group_name": d.group_name or "",
            "is_simulated": d.is_simulated,
            "primary_ip": ip,
            "primary_credential_id": cred_id,
            "created_at": (d.created_at or "").isoformat() if d.created_at else "",
            "updated_at": (d.updated_at or "").isoformat() if d.updated_at else "",
        })
    return rows


def _credentials(db: Session, since: str | None) -> list[dict[str, Any]]:
    from app.models.credential import Credential
    q = db.query(Credential).order_by(Credential.id)
    if since:
        q = q.filter(Credential.updated_at >= since)
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description or "",
            "username": c.username,
            "secret_kind": c.secret_kind,
            "created_at": (c.created_at or "").isoformat() if c.created_at else "",
            "updated_at": (c.updated_at or "").isoformat() if c.updated_at else "",
            # Note: Credential is a vault — it is NOT linked directly to devices.
            # Devices reference credentials via DeviceEndpoint.credential_id (1→N).
        }
        for c in q.all()
    ]


def _configs(db: Session, since: str | None) -> list[dict[str, Any]]:
    from app.models.config import ConfigArchive, ConfigRevision
    archives = db.query(ConfigArchive).order_by(ConfigArchive.id)
    if since:
        archives = archives.filter(ConfigArchive.created_at >= since)
    out = []
    for a in archives.all():
        revs = (
            db.query(ConfigRevision)
            .filter_by(archive_id=a.id)
            .order_by(ConfigRevision.revision_no)
            .all()
        )
        for rev in revs:
            out.append(
                {
                    "archive_id": a.id,
                    "device_id": a.device_id,
                    "name": a.name if hasattr(a, "name") else f"archive-{a.id}",
                    "revision_no": rev.revision_no,
                    "source": rev.source or "",
                    "author": rev.author or "",
                    "note": rev.note or "",
                    "created_at": (rev.created_at or "").isoformat()
                    if rev.created_at
                    else "",
                    "size_bytes": rev.size_bytes or 0,
                    "content_hash": rev.content_hash or "",
                    "storage_path": rev.storage_path or "",
                }
            )
    return out


def _documents(db: Session, since: str | None) -> list[dict[str, Any]]:
    from app.models.document import Document
    q = db.query(Document).order_by(Document.id)
    if since:
        q = q.filter(Document.created_at >= since)
    return [
        {
            "id": d.id,
            "title": d.title,
            "vendor": d.vendor or "",
            "category": d.category or "",
            "version_tag": d.version_tag or "",
            "filename": d.filename,
            "size_bytes": d.size_bytes,
            "content_hash": d.content_hash,
            "summary": d.summary or "",
            "source": d.source or "",
            "uploaded_by": d.uploaded_by,
            "index_status": d.index_status,
            "index_error": d.index_error or "",
            "created_at": (d.created_at or "").isoformat() if d.created_at else "",
        }
        for d in q.all()
    ]


def _drift(db: Session, since: str | None) -> list[dict[str, Any]]:
    from app.models.drift import ConfigDrift
    q = db.query(ConfigDrift).order_by(ConfigDrift.id.desc())
    if since:
        q = q.filter(ConfigDrift.detected_at >= since)
    out = []
    for d in q.all():
        snippet = d.diff_snippet or ""
        out.append(
            {
                "id": d.id,
                "device_id": d.device_id,
                "baseline_revision_id": d.baseline_revision_id,
                "current_revision_id": d.current_revision_id,
                "severity": d.severity or "",
                "status": d.status or "",
                "lines_added": d.lines_added or 0,
                "lines_removed": d.lines_removed or 0,
                "lines_modified": d.lines_modified or 0,
                "baseline_hash": d.baseline_hash or "",
                "current_hash": d.current_hash or "",
                "diff_snippet": snippet,
                "change_hint": d.change_hint or "",
                "acknowledged_by": d.acknowledged_by or "",
                "acknowledged_at": (d.acknowledged_at or "").isoformat() if d.acknowledged_at else "",
                "detected_at": (d.detected_at or "").isoformat() if d.detected_at else "",
            }
        )
    return out


def _audit(db: Session, since: str | None, days: int = 365) -> list[dict[str, Any]]:
    from app.models.audit import AuditEvent
    q = db.query(AuditEvent).order_by(AuditEvent.id.desc())
    if since:
        q = q.filter(AuditEvent.created_at >= since)
    else:
        from datetime import timedelta
        q = q.filter(
            AuditEvent.created_at >= datetime.now(timezone.utc) - timedelta(days=days)
        )
    return [
        {
            "id": a.id,
            "action": a.action,
            "actor": a.actor or "",
            "target_kind": a.target_kind or "",
            "target_id": a.target_id or "",
            "detail": a.detail or "",
            "ip": a.ip_address or "",
            "created_at": (a.created_at or "").isoformat() if a.created_at else "",
        }
        for a in q.all()
    ]


# ─────────────────────────────────────────────────────────────────────
# Scope map
# ─────────────────────────────────────────────────────────────────────

_SCOPES: dict[str, callable] = {
    "devices":    _devices,
    "credentials": _credentials,
    "configs":    _configs,
    "documents":  _documents,
    "drift":      _drift,
    "audit":      _audit,
}


# ─────────────────────────────────────────────────────────────────────
# JSON export
# ─────────────────────────────────────────────────────────────────────

@router.get("/export/{scope}")
def export_json(
    scope: str,
    format: str = Query("json"),
    since: str | None = Query(None, description="ISO-8601 datetime; only records after this time"),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export one or all scopes as JSON or CSV/ZIP.

    * ``GET /export/devices?format=json`` — one scope, plain JSON file
    * ``GET /export/all?format=json`` — all scopes, JSON
    * ``GET /export/all?format=csv`` — all scopes, ZIP of CSVs
    """
    if scope not in _SCOPES and scope != "all":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Unknown scope: {scope}")

    if format == "csv" and scope != "all":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="CSV format is only available for scope=all (ZIP with one CSV per scope)",
        )

    timestamp = _dt()

    # Collect data
    if scope == "all":
        data = {k: fn(db, since) for k, fn in _SCOPES.items()}
        data["exported_at"] = timestamp
        data["exported_by"] = _user.username
    else:
        data = _SCOPES[scope](db, since)
        data = {"scope": scope, "exported_at": timestamp, "exported_by": _user.username, "records": data}

    if format == "json":
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        filename = f"naw-export-{scope}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        return StreamingResponse(
            io.BytesIO(body),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    # CSV / ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if scope == "all":
            for name, rows in data.items():
                if name in ("exported_at", "exported_by") or not rows:
                    continue
                zf.writestr(f"{name}.csv", _rows_to_csv(rows, f"{name}.csv"))
        else:
            zf.writestr(f"{scope}.csv", _rows_to_csv(data["records"], f"{scope}.csv"))
            # Include metadata
            meta = {"scope": scope, "exported_at": timestamp, "exported_by": _user.username}
            zf.writestr("_meta.json", json.dumps(meta, ensure_ascii=False))
    buf.seek(0)
    filename = f"naw-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
