"""Job-related schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.plan import ConfigPlan, PlanSource


class JobCreate(BaseModel):
    """Job creation payload — accepts either legacy or new ConfigPlan form.

    **Legacy form** (existing clients): set ``commands`` /
    ``template_id`` + ``params``. No ``plan`` block.

    **New form** (post-pivot): set ``plan`` to a ConfigPlan-shaped
    dict, omit ``commands`` + ``template_id``. The handler in
    ``api/v1/jobs.py`` prefers ``plan`` when present and falls back to
    the legacy path otherwise — so old frontends stay green while new
    ones get preview == execution parity.

    ``plan.source`` carries provenance (manual / template / ai_script /
    ai_chat) so the audit log can answer "where did this come from?".
    """
    name: str
    kind: str
    target_device_ids: list[int] = Field(default_factory=list)
    template_id: Optional[int] = None
    params: Optional[dict] = None
    note: Optional[str] = None
    batch_size: int = 5
    # New: pre-built unified plan. If supplied, takes precedence over
    # template_id/commands and gets stored on Job.params['__plan__'].
    plan: Optional[dict] = None
    # Convenience: source override for legacy callers that don't want
    # to construct a full plan. Defaults to "manual".
    source: Optional[str] = None
    # Custom commands to run on each target device (kind="batch_read" only).
    # The previous JobsPage UI omitted this field entirely, so every
    # batch_read job stored an empty command list and the worker fell
    # back to the hardcoded "display current-configuration" — which is
    # why "启动" silently did the wrong thing. Now the UI sends a
    # user-authored command list and the worker honors it.
    # See api/v1/jobs.py:create_job and workers/tasks.py:batch_collect
    # for how this flows through.
    commands: Optional[list[str]] = None


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
    """Dry-run payload — same dual-shape policy as JobCreate.

    Either:
      * ``plan``             — full ConfigPlan dict (preferred; the
        preview *sees exactly what the Worker will execute*)
      * ``commands`` + ``params`` + ``template_id`` — legacy form
    """
    commands: list[str] = Field(default_factory=list)
    template_id: Optional[int] = None
    params: Optional[dict] = None
    device_ids: list[int] = Field(default_factory=list)
    plan: Optional[dict] = None


class DryRunCommand(BaseModel):
    device_id: int
    commands: list[str]


class DryRunOut(BaseModel):
    job_kind: str
    commands_by_device: list[DryRunCommand]
    risk: dict
    estimated_minutes: int
    notes: list[str] = Field(default_factory=list)
