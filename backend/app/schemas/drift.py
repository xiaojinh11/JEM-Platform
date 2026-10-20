"""Pydantic schemas for config drift."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DriftSeverityCount(BaseModel):
    critical: int = 0
    warning: int = 0
    info: int = 0


class DriftSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int
    new_count: int
    acknowledged: int
    by_severity: DriftSeverityCount


class DriftDeviceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: int
    device_name: str
    vendor: str
    group_name: Optional[str]
    last_drift_at: Optional[datetime]
    drift_count: int
    latest_severity: Optional[str]


class DriftListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_name: str
    vendor: str
    group_name: Optional[str]
    detected_at: datetime
    severity: str
    status: str
    lines_added: int
    lines_removed: int
    lines_modified: int
    change_hint: Optional[str]
    acknowledged_by: Optional[str]
    acknowledged_at: Optional[datetime]


class DriftDetail(DriftListItem):
    baseline_revision_id: Optional[int]
    current_revision_id: Optional[int]
    diff_snippet: Optional[str]
    baseline_hash: Optional[str]
    current_hash: Optional[str]


class AcknowledgeBody(BaseModel):
    pass  # just uses the authenticated user from the JWT token


class SetBaselineBody(BaseModel):
    revision_id: Optional[int] = None  # None → use live config as baseline


class ScanTriggerOut(BaseModel):
    task_id: str
    message: str
