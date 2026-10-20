"""Device CRUD + CSV import + endpoint management."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, require_operator
from app.db.session import get_db
from app.models.device import Device, DeviceEndpoint
from app.models.user import User
from app.schemas.device import BulkImportResult, DeviceIn, DeviceOut, DeviceListItem, DeviceEndpointOut, DeviceEndpointIn, DeviceUpdateIn
from app.services.devices import (
    bulk_import_from_csv,
    create_or_update_device,
    delete_device,
    update_device,
    list_devices,
    get_primary_endpoint,
    add_endpoint,
    delete_endpoint,
    set_primary_endpoint,
)
from app.services.audit import record as audit_record

router = APIRouter()


@router.get("", response_model=list[DeviceListItem])
def get_devices(
    q: str | None = Query(None),
    group: str | None = Query(None),
    vendor: str | None = Query(None),
    role: str | None = Query(None),
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    devices = list_devices(db, q=q, group=group, vendor=vendor, role=role, include_inactive=include_inactive)
    items = []
    for d in devices:
        ep = get_primary_endpoint(db, d.id)
        items.append(
            DeviceListItem(
                id=d.id,
                name=d.name,
                hostname=d.hostname,
                vendor=d.vendor,
                role=d.role,
                group_name=d.group_name,
                location=d.location,
                is_simulated=d.is_simulated,
                is_active=d.is_active,
                primary_endpoint=DeviceEndpointOut.model_validate(ep) if ep else None,
            )
        )
    return items


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="device not found")
    return dev


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(payload: DeviceIn, _user=Depends(require_operator), db: Session = Depends(get_db)):
    # NOTE: ``is_simulated`` is intentionally NOT taken from the payload —
    # it's derived from ``vendor`` inside ``create_or_update_device``.
    # This blocks the path where an operator could tag a real production
    # device as "simulated" and have every batch job silently skip the
    # SSH transport.
    dev, _ = create_or_update_device(
        db,
        name=payload.name,
        vendor=payload.vendor,
        hostname=payload.hostname,
        model=payload.model,
        software_version=payload.software_version,
        role=payload.role,
        serial_no=payload.serial_no,
        location=payload.location,
        tags=payload.tags,
        group_name=payload.group_name,
        notes=payload.notes,
    )
    for ep_in in payload.endpoints:
        add_endpoint(
            db,
            device_id=dev.id,
            kind=ep_in.kind,
            address=ep_in.address,
            port=ep_in.port,
            credential_id=ep_in.credential_id,
            is_primary=ep_in.is_primary,
            notes=ep_in.notes,
        )
    audit_record(db, actor=_user.username, action="device.create", target_kind="device", target_id=str(dev.id), detail=dev.name)
    db.commit()
    db.refresh(dev)
    return dev


@router.delete("/{device_id}", status_code=204)
def remove_device(device_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="device not found")
    delete_device(db, dev)
    return None


@router.patch("/{device_id}", response_model=DeviceOut)
def patch_device(
    device_id: int,
    payload: DeviceUpdateIn,
    db: Session = Depends(get_db),
    _user: User = Depends(require_operator),
):
    """Edit a device's editable fields. Only operator+ can patch.

    Whitelisted fields are applied verbatim; unknown / non-whitelisted
    keys are silently dropped (defense in depth — the schema also
    strips them).  Returns the refreshed device so the UI can re-render
    without a follow-up GET.
    """
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="device not found")
    # ``model_dump(exclude_unset=True)`` keeps only the keys the caller
    # actually sent — distinguishes "leave as is" (key absent) from
    # "set to None" (key present with value null).
    patch = payload.model_dump(exclude_unset=True)
    before = {k: getattr(dev, k) for k in patch.keys()}
    update_device(db, dev, patch)
    audit_record(
        db,
        actor=_user.username,
        action="device.update",
        target_kind="device",
        target_id=str(dev.id),
        detail=str({k: {"before": before[k], "after": patch[k]} for k in patch.keys()}),
    )
    db.commit()
    db.refresh(dev)
    return dev


@router.post("/import-csv", response_model=BulkImportResult)
def import_csv(
    file: UploadFile,
    default_vendor: str = Query("huawei_vrp"),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="must be a CSV file")
    content = file.file.read()
    result = bulk_import_from_csv(db, content, default_vendor=default_vendor)
    audit_record(db, actor=_admin.username, action="device.bulk_import", detail=f"total={result.total} inserted={result.inserted}")
    db.commit()
    return result


# ── Endpoint CRUD (needed by Device Detail page for CRT-style connection config) ──

@router.get(
    "/{device_id}/endpoints",
    response_model=list[DeviceEndpointOut],
    summary="List all connection endpoints for a device",
)
def list_device_endpoints(
    device_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="device not found")
    eps = (
        db.query(DeviceEndpoint)
        .filter(DeviceEndpoint.device_id == device_id)
        .order_by(DeviceEndpoint.is_primary.desc(), DeviceEndpoint.id.asc())
        .all()
    )
    return eps


@router.post(
    "/{device_id}/endpoints",
    response_model=DeviceEndpointOut,
    status_code=201,
    summary="Add a new endpoint to the device",
)
def create_device_endpoint(
    device_id: int,
    payload: DeviceEndpointIn,
    _user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="device not found")
    ep = add_endpoint(
        db,
        device_id=device_id,
        kind=payload.kind,
        address=payload.address,
        port=payload.port,
        credential_id=payload.credential_id,
        is_primary=payload.is_primary,
        notes=payload.notes,
    )
    audit_record(
        db,
        actor=_user.username,
        action="device.endpoint.create",
        target_kind="device",
        target_id=str(device_id),
        detail=f"{payload.kind} {payload.address}:{payload.port}",
    )
    db.commit()
    db.refresh(ep)
    return ep


@router.delete(
    "/{device_id}/endpoints/{endpoint_id}",
    status_code=204,
    summary="Delete a connection endpoint",
)
def remove_device_endpoint(
    device_id: int,
    endpoint_id: int,
    _user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    ep = db.get(DeviceEndpoint, endpoint_id)
    if not ep or ep.device_id != device_id:
        raise HTTPException(status_code=404, detail="endpoint not found")
    detail = f"{ep.kind} {ep.address}:{ep.port}"
    delete_endpoint(db, ep)
    audit_record(
        db,
        actor=_user.username,
        action="device.endpoint.delete",
        target_kind="device",
        target_id=str(device_id),
        detail=detail,
    )
    db.commit()
    return None


@router.post(
    "/{device_id}/endpoints/{endpoint_id}/primary",
    response_model=DeviceEndpointOut,
    summary="Mark endpoint as primary (used by WebSSH quick-connect)",
)
def make_endpoint_primary(
    device_id: int,
    endpoint_id: int,
    _user=Depends(require_operator),
    db: Session = Depends(get_db),
):
    ep = db.get(DeviceEndpoint, endpoint_id)
    if not ep or ep.device_id != device_id:
        raise HTTPException(status_code=404, detail="endpoint not found")
    ep = set_primary_endpoint(db, device_id, endpoint_id)
    audit_record(
        db,
        actor=_user.username,
        action="device.endpoint.set_primary",
        target_kind="device",
        target_id=str(device_id),
        detail=f"endpoint_id={endpoint_id}",
    )
    db.commit()
    db.refresh(ep)
    return ep
