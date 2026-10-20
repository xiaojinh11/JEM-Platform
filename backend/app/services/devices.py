"""Device import + listing service."""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.enums import EndpointKind, Vendor
from app.core.logging import logger
from app.models.device import Device, DeviceEndpoint
from app.schemas.device import BulkImportResult, DEVICE_PATCHABLE_FIELDS
from app.utils.helpers import parse_csv_devices


def _derive_is_simulated(vendor: str, address: Optional[str] = None) -> bool:
    """Single source of truth for the "is this device real?" flag.

    The flag is *derived*, never user-provided on the API path — see the
    note on ``DeviceIn``.  The ``address`` heuristic is kept for the CSV
    import path for backwards-compatibility with spreadsheets that use
    ``sim`` / ``simulated`` as a placeholder address.
    """
    if (vendor or "").lower() == Vendor.SIMULATED.value:
        return True
    if address and address.lower() in {"sim", "simulated"}:
        return True
    return False


def create_or_update_device(
    db: Session,
    *,
    name: str,
    vendor: str,
    hostname: Optional[str] = None,
    model: Optional[str] = None,
    software_version: Optional[str] = None,
    role: str = "switch",
    serial_no: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[str] = None,
    group_name: Optional[str] = None,
    notes: Optional[str] = None,
    # Kept as a kwarg for the CSV-import path, which derives it from
    # vendor/address.  Callers from the API must NOT pass this; the
    # default keeps the API surface tight.
    is_simulated: Optional[bool] = None,
) -> tuple[Device, bool]:
    """Return (device, created).

    ``is_simulated`` is derived from ``vendor`` (and ``address`` if known)
    when not supplied.  API callers should let the service do the
    derivation so the flag can never be forged.
    """
    derived_simulated = _derive_is_simulated(vendor) if is_simulated is None else bool(is_simulated)

    existing = db.query(Device).filter_by(name=name).one_or_none()
    if existing:
        for field, value in (
            ("vendor", vendor),
            ("hostname", hostname or name),
            ("model", model),
            ("software_version", software_version),
            ("role", role),
            ("serial_no", serial_no),
            ("location", location),
            ("tags", tags),
            ("group_name", group_name),
            ("notes", notes),
            # NOTE: ``is_simulated`` is intentionally NOT mutated on
            # existing devices via this path — the flag is set when the
            # device is first created (via derivation) and stays put
            # unless an admin uses a dedicated migration/console tool.
            # Re-syncing it here would silently re-classify a device
            # every time the operator edits any field.
        ):
            setattr(existing, field, value)
        return existing, False
    dev = Device(
        name=name,
        hostname=hostname or name,
        vendor=vendor,
        model=model,
        software_version=software_version,
        role=role,
        serial_no=serial_no,
        location=location,
        tags=tags,
        group_name=group_name,
        notes=notes,
        is_simulated=derived_simulated,
    )
    db.add(dev)
    db.flush()
    return dev, True


def add_endpoint(
    db: Session,
    *,
    device_id: int,
    kind: str,
    address: str,
    port: int,
    credential_id: Optional[int] = None,
    is_primary: bool = True,
    notes: Optional[str] = None,
) -> DeviceEndpoint:
    # If this endpoint is being marked primary, demote any existing primary
    # endpoint on the same device so that ``is_primary`` is a singleton.
    if is_primary:
        (
            db.query(DeviceEndpoint)
            .filter_by(device_id=device_id, is_primary=True)
            .update({DeviceEndpoint.is_primary: False}, synchronize_session=False)
        )
    ep = DeviceEndpoint(
        device_id=device_id,
        kind=kind,
        address=address,
        port=port,
        credential_id=credential_id,
        is_primary=is_primary,
        notes=notes,
    )
    db.add(ep)
    return ep


def delete_endpoint(db: Session, ep: DeviceEndpoint) -> None:
    """Delete a DeviceEndpoint and, if it was primary, promote another one."""
    device_id = ep.device_id
    was_primary = ep.is_primary
    db.delete(ep)
    db.flush()
    if was_primary:
        # Promote the oldest remaining endpoint to primary so the device
        # always has a usable default for WebSSH quick-connect.
        next_ep = (
            db.query(DeviceEndpoint)
            .filter(DeviceEndpoint.device_id == device_id)
            .order_by(DeviceEndpoint.id.asc())
            .first()
        )
        if next_ep is not None:
            next_ep.is_primary = True


def set_primary_endpoint(
    db: Session, device_id: int, endpoint_id: int
) -> DeviceEndpoint:
    """Mark ``endpoint_id`` as primary, demoting all others for the device."""
    (
        db.query(DeviceEndpoint)
        .filter(DeviceEndpoint.device_id == device_id, DeviceEndpoint.is_primary.is_(True))
        .update({DeviceEndpoint.is_primary: False}, synchronize_session=False)
    )
    target = db.get(DeviceEndpoint, endpoint_id)
    if target is not None:
        target.is_primary = True
    db.flush()
    return target


def list_devices(
    db: Session,
    *,
    q: Optional[str] = None,
    group: Optional[str] = None,
    vendor: Optional[str] = None,
    role: Optional[str] = None,
    include_inactive: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[Device]:
    query = db.query(Device)
    if not include_inactive:
        query = query.filter(Device.is_active.is_(True))
    if group:
        query = query.filter(Device.group_name == group)
    if vendor:
        query = query.filter(Device.vendor == vendor)
    if role:
        query = query.filter(Device.role == role)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Device.name.ilike(like),
                Device.hostname.ilike(like),
                Device.serial_no.ilike(like),
                Device.location.ilike(like),
                Device.tags.ilike(like),
            )
        )
    return query.order_by(Device.name.asc()).limit(limit).offset(offset).all()


def get_primary_endpoint(db: Session, device_id: int) -> Optional[DeviceEndpoint]:
    return (
        db.query(DeviceEndpoint)
        .filter_by(device_id=device_id, is_primary=True)
        .first()
    )


def delete_device(db: Session, device: Device) -> None:
    db.delete(device)
    db.commit()


def update_device(
    db: Session,
    device: Device,
    patch: dict,
) -> Device:
    """Apply a whitelisted PATCH to a device.

    Caller is responsible for validating that the patch dict only
    contains allowed keys (we filter through ``DEVICE_PATCHABLE_FIELDS``
    anyway, so a malicious payload is harmless).  Empty / None values
    are skipped — this lets the UI send only the fields it actually
    changed.  Explicit ``None`` for ``notes`` etc. is honored because
    the UI may want to clear a field; to truly skip a field, omit the
    key from the payload.
    """
    for key, value in patch.items():
        if key not in DEVICE_PATCHABLE_FIELDS:
            # Defensive: a future schema field added without
            # updating the whitelist must not silently leak through.
            continue
        setattr(device, key, value)
    db.flush()
    return device


def bulk_import_from_csv(db: Session, csv_bytes: bytes, *, default_vendor: str = "huawei_vrp") -> BulkImportResult:
    rows = parse_csv_devices(csv_bytes)
    inserted = 0
    updated = 0
    errors: list[str] = []

    def _as_int(text: Optional[str], default: int) -> int:
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            return default

    def _split_tags(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        return ",".join(t.strip() for t in re.split(r"[;,]", text) if t.strip())

    import re

    for row in rows:
        try:
            name = row["name"]
            vendor = row.get("vendor") or default_vendor
            existing = db.query(Device).filter_by(name=name).one_or_none()
            address = row.get("address") or row.get("mgmt_ip")
            # CSV preserves the legacy "address in {sim, simulated}"
            # heuristic; everything else goes through the standard
            # derivation in ``create_or_update_device``.
            is_simulated = _derive_is_simulated(vendor, address)
            dev, created = create_or_update_device(
                db,
                name=name,
                vendor=vendor,
                hostname=row.get("hostname") or name,
                model=row.get("model"),
                software_version=row.get("software_version") or row.get("version"),
                role=row.get("role") or "switch",
                serial_no=row.get("serial_no"),
                location=row.get("location"),
                tags=_split_tags(row.get("tags")),
                group_name=row.get("group") or row.get("group_name"),
                notes=row.get("notes"),
                is_simulated=is_simulated,
            )
            if created:
                inserted += 1
            else:
                updated += 1
            if address and not any(ep.address == address for ep in dev.endpoints):
                add_endpoint(
                    db,
                    device_id=dev.id,
                    kind=row.get("endpoint_kind") or EndpointKind.MGMT_SSH.value,
                    address=address,
                    port=_as_int(row.get("port"), 22),
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{row.get('name', '<unknown>')}: {exc}")

    db.commit()
    return BulkImportResult(total=len(rows), inserted=inserted, updated=updated, errors=errors)
