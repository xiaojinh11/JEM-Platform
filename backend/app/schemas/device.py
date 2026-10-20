"""Device + endpoint schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DeviceEndpointIn(BaseModel):
    kind: str = Field(..., description="inband_ssh | mgmt_ssh | console | restconf | netconf")
    address: str
    port: int = 22
    credential_id: Optional[int] = None
    is_primary: bool = True
    notes: Optional[str] = None


class DeviceEndpointOut(DeviceEndpointIn):
    id: int

    model_config = {"from_attributes": True}


class DeviceIn(BaseModel):
    name: str = Field(..., max_length=128)
    hostname: Optional[str] = Field(None, max_length=128)
    vendor: str = Field(..., max_length=32)
    model: Optional[str] = Field(None, max_length=64)
    software_version: Optional[str] = Field(None, max_length=64)
    role: str = Field(default="switch", max_length=32)
    serial_no: Optional[str] = Field(None, max_length=64)
    location: Optional[str] = Field(None, max_length=128)
    tags: Optional[str] = Field(None, max_length=512)
    group_name: Optional[str] = Field(None, max_length=64)
    notes: Optional[str] = Field(None, max_length=5000)  # Prevent oversized payloads
    # ``is_simulated`` is intentionally NOT in the input schema: it is
    # derived from ``vendor`` (and, for CSV imports, from the legacy
    # ``address`` heuristic) and cannot be set via API.  Allowing it to be
    # passed in would let an operator bypass real SSH on a production
    # device by tagging it as "simulated" — every executor path treats the
    # flag as a signal to skip the transport and return a fake success.
    endpoints: list[DeviceEndpointIn] = Field(default_factory=list)


class DeviceUpdateIn(BaseModel):
    """PATCH payload — every field optional so the caller can edit one
    thing at a time.  Vendor and is_simulated are deliberately excluded
    (vendor can't change, is_simulated is derived).  Endpoints are
    managed via the separate /endpoints endpoints."""

    name: Optional[str] = Field(None, max_length=128)
    hostname: Optional[str] = Field(None, max_length=128)
    model: Optional[str] = Field(None, max_length=64)
    software_version: Optional[str] = Field(None, max_length=64)
    role: Optional[str] = Field(None, max_length=32)
    serial_no: Optional[str] = Field(None, max_length=64)
    location: Optional[str] = Field(None, max_length=128)
    tags: Optional[str] = Field(None, max_length=512)
    group_name: Optional[str] = Field(None, max_length=64)
    notes: Optional[str] = Field(None, max_length=5000)
    is_active: Optional[bool] = None


# Whitelist of fields a PATCH can touch.  Anything else in the payload
# is silently dropped — keeps the API from letting operators bypass
# the "is_simulated is derived" rule by smuggling it through PATCH.
DEVICE_PATCHABLE_FIELDS = frozenset({
    "name",
    "hostname",
    "model",
    "software_version",
    "role",
    "serial_no",
    "location",
    "tags",
    "group_name",
    "notes",
    "is_active",
})


class DeviceOut(DeviceIn):
    id: int
    created_at: datetime
    updated_at: datetime
    is_simulated: bool  # derived, read-only

    model_config = {"from_attributes": True}


class DeviceListItem(BaseModel):
    id: int
    name: str
    hostname: Optional[str] = None
    vendor: str
    role: str
    group_name: Optional[str] = None
    location: Optional[str] = None
    is_simulated: bool
    is_active: bool
    primary_endpoint: Optional[DeviceEndpointOut] = None

    model_config = {"from_attributes": True}


class BulkImportResult(BaseModel):
    total: int
    inserted: int
    updated: int
    errors: list[str]
