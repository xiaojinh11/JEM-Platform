"""Unified configuration-plan data model.

A ``ConfigPlan`` is the **single source of truth** for "what is about to
be applied". The downstream pipelines all consume it:

  1. AI script parsing -> ``app.services.plan_builder.build_plan_from_script``
  2. Manual user input -> ``build_plan_from_inputs``
  3. Template render   -> ``build_plan_from_template``
  4. Dry-run preview   -> POST /jobs/dry-run (returns PlanRisk + ETA)
  5. Job creation      -> POST /jobs         (stores plan on Job.params)
  6. Worker execution  -> reads back from Job.params

Design rules
------------

* The plan is a frozen, serializable Pydantic model — not a custom
  class. We store it as JSON inside ``Job.params`` under the key
  ``__plan__``.
* ``commands`` are the *rendered* device commands, never raw template
  body. Anything that survives into a Job must be readable text — this
  is the audit-of-record.
* ``source`` is mandatory. The risk layer treats ``ai_script`` and
  ``ai_chat`` more conservatively than ``manual``.
* ``risk`` is filled by the rule layer on the way in. AI may
  *augment* ``ai_comment`` but may NOT downgrade ``requires_approval``.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Risk block ──────────────────────────────────────────────────────


class PlanRisk(BaseModel):
    """Risk verdict attached to a ConfigPlan."""

    level: str = "safe"  # safe | caution | dangerous
    matched: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    ai_comment: Optional[str] = None
    blast_radius: dict[str, Any] = Field(default_factory=dict)

    @field_validator("level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        if v not in {"safe", "caution", "dangerous"}:
            raise ValueError(f"invalid risk level: {v!r}")
        return v


# ── Source metadata (provenance) ────────────────────────────────────


class PlanSource(BaseModel):
    """Where did this plan come from?

    Source enum values:
      * manual      — operator typed commands directly
      * template    — rendered from a saved template
      * ai_script   — extracted from an uploaded Python script
      * ai_chat     — proposed by the AI assistant in a chat session
    """

    source: str = "manual"
    script_id: Optional[int] = None
    template_id: Optional[int] = None
    chat_session_id: Optional[str] = None
    note: Optional[str] = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v not in {"manual", "template", "ai_script", "ai_chat"}:
            raise ValueError(f"invalid source: {v!r}")
        return v


# ── The plan itself ─────────────────────────────────────────────────


class ConfigPlan(BaseModel):
    """One deployable configuration plan."""

    plan_id: str = ""
    vendor: str = ""
    device_ids: list[int] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    source: PlanSource = Field(default_factory=PlanSource)
    risk: PlanRisk = Field(default_factory=PlanRisk)
    meta: dict[str, Any] = Field(default_factory=dict)
    """
    Extra metadata:
      - estimated_minutes: int
      - estimated_at: iso (when the estimate was computed)
      - notes: list[str]    (advisory strings from the rule layer)
      - template_name: str  (only when source.source == "template")
      - parser_warnings: list[str] (only when source.source == "ai_script")
      - requires_review: bool      (only when source.source == "ai_script")
    """

    def command_count(self) -> int:
        return len(self.commands)

    def device_count(self) -> int:
        return len(self.device_ids)

    def to_storage_dict(self) -> dict[str, Any]:
        """Serialize for ``Job.params['__plan__']``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_storage_dict(cls, raw: dict[str, Any]) -> "ConfigPlan":
        return cls.model_validate(raw)


# ── Helpers ─────────────────────────────────────────────────────────


def empty_risk() -> PlanRisk:
    return PlanRisk(level="safe", matched=[], requires_approval=False)


def wraps_for_storage(plan: ConfigPlan) -> dict[str, Any]:
    """Build the full ``Job.params`` blob: legacy keys + new ``__plan__``.

    Existing workers still read ``params.get('__inline_commands')`` and
    ``params.get('template_id')``; we keep those populated for
    backward compatibility. New code should read ``plan =
    params['__plan__']`` instead — same plan the user previewed.
    """
    params: dict[str, Any] = dict(plan.meta or {})
    if plan.source.template_id is not None:
        params["template_id"] = plan.source.template_id
    if plan.commands:
        params["__inline_commands"] = "\n".join(plan.commands)
    params["__plan__"] = plan.to_storage_dict()
    return params
