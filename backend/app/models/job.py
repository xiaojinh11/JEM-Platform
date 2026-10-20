"""Job, JobStep, and BackupSnapshot models for batch operations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # batch_read | batch_write | template_apply | backup | config_diff
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    target_device_ids: Mapped[list] = mapped_column(JSON, default=list)
    template_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    params: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    initiated_by: Mapped[str] = mapped_column(String(64))
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    note: Mapped[Optional[str]] = mapped_column(Text, default=None)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)  # High-risk flag
    approved_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    # ── D: AI-driven change analysis ──────────────────────────
    # risk_assessment = {
    #   level: "safe" | "caution" | "dangerous",
    #   score: 0..100,                          # higher = riskier
    #   factors: [{...}],                        # what made it risky
    #   affected_devices: [...],                 # blast radius from graph
    #   dry_run_summary: "...",                  # AI-generated human summary
    #   model: "...", tokens: int, generated_at: iso
    # }
    risk_assessment: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    risk_assessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    risk_assessment_error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # rollback_plan = {
    #   strategy: "manual" | "auto_backup_restore" | "config_patch",
    #   steps: [...],                            # ordered human-readable steps
    #   estimated_minutes: int,
    #   dependencies: [...],                     # other things to revert first
    #   generated_at: iso
    # }
    rollback_plan: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    # ai_comparison = {                          # filled after job completes
    #   expected: "...",
    #   actual: "...",
    #   deltas: [...],                           # unexpected diffs
    #   warnings: [...],
    #   verdict: "as_expected" | "minor_diff" | "major_diff" | "failed",
    #   generated_at: iso
    # }
    ai_comparison: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    ai_comparison_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)

    steps: Mapped[list["JobStep"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    backups: Mapped[list["BackupSnapshot"]] = relationship(back_populates="job", cascade="all, delete-orphan")

    # ── Performance: composite indexes for hot dashboard / list queries ─
    # The pending-approval counter on /dashboard runs:
    #     WHERE requires_approval IS TRUE AND approved_by IS NULL
    #       AND status IN ('draft', 'pending')
    # Without this composite, Postgres has to merge three single-column
    # indexes via bitmap-AND.  With it, it's a direct index seek.
    __table_args__ = (
        Index("ix_jobs_pending_approval", "requires_approval", "approved_by", "status"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )


class JobStep(Base):
    __tablename__ = "job_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    step_kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    job: Mapped["Job"] = relationship(back_populates="steps")


class BackupSnapshot(Base):
    __tablename__ = "backup_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), default=None)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(32))  # before | after | manual | rollback
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    job: Mapped["Job"] = relationship(back_populates="backups")
