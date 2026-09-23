"""Job-related schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    name: str
    kind: str
    target_device_ids: list[int] = Field(default_factory=list)
    template_id: Optional[int] = None
    params: Optional[dict] = None
    note: Optional[str] = None
    batch_size: int = 5


class JobOut(BaseModel):
    id: int
    name: str
    kind: str
    status: str
    target_device_ids: list[int] = Field(default_factory=list)
    template_id: Optional[int] = None
    summary: Optional[dict] = None
    initiated_by: str
    batch_size: int
    celery_task_id: Optional[str] = None
    note: Optional[str] = None
    requires_approval: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    risk_assessment: Optional[dict] = None
    risk_assessed_at: Optional[datetime] = None
    risk_assessment_error: Optional[str] = None
    rollback_plan: Optional[dict] = None
    ai_comparison: Optional[dict] = None
    ai_comparison_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class JobStepOut(BaseModel):
    id: int
    device_id: int
    step_kind: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class JobDetail(JobOut):
    steps: list[JobStepOut] = Field(default_factory=list)


class JobControlRequest(BaseModel):
    action: str = Field(..., description="start | cancel")


class DryRunRequest(BaseModel):
    commands: list[str] = Field(default_factory=list)
    template_id: Optional[int] = None
    params: Optional[dict] = None
    device_ids: list[int] = Field(default_factory=list)


class DryRunCommand(BaseModel):
    device_id: int
    commands: list[str]


class DryRunOut(BaseModel):
    job_kind: str
    commands_by_device: list[DryRunCommand]
    risk: dict
    estimated_minutes: int
    notes: list[str] = Field(default_factory=list)
