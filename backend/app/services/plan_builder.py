"""Builders for ``ConfigPlan``.

One builder per source. All three return a plan with the same shape.
After a plan is built the caller MUST run ``assess_plan_risk`` before
using it for a Dry-Run or creating a Job — otherwise the plan's
``risk`` block stays ``safe`` and the approval gate is bypassed.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.template import Template
from app.schemas.plan import ConfigPlan, PlanRisk, PlanSource, empty_risk
from app.services.ai_risk import _rule_based_risk
from app.services.templates import TemplateError_, extract_commands, render


# Vendor detection (heuristic). Hint only, not authoritative.

_VENDOR_HINTS = {
    "huawei_vrp": ["huawei", "vrp", "vlanif", "system-view", "quit",
                   "return", "display this"],
    "cisco_ios_xe": ["cisco", "configure terminal", "show running-config",
                     "write memory", "no shutdown", "ip address"],
    "ruijie_rgos": ["ruijie", "rgos", "configure terminal",
                    "show running-config"],
    "h3c_comware": ["h3c", "comware", "system-view", "quit"],
}


def detect_vendor(commands: list[str]) -> str:
    if not commands:
        return ""
    blob = "\n".join(commands).lower()
    scores = {v: sum(1 for h in hints if h.lower() in blob)
              for v, hints in _VENDOR_HINTS.items()}
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else ""


def _new_plan_id() -> str:
    return uuid.uuid4().hex


def build_plan_from_inputs(
    *,
    commands: list[str],
    device_ids: list[int],
    vendor: str = "",
    meta: Optional[dict[str, Any]] = None,
) -> ConfigPlan:
    return ConfigPlan(
        plan_id=_new_plan_id(),
        vendor=vendor or detect_vendor(commands),
        device_ids=list(device_ids),
        commands=list(commands),
        source=PlanSource(source="manual"),
        risk=empty_risk(),
        meta=dict(meta or {}),
    )


def build_plan_from_template(
    db: Session,
    *,
    template_id: int,
    params: Optional[dict[str, Any]],
    device_ids: list[int],
) -> ConfigPlan:
    tpl = db.get(Template, template_id)
    if not tpl:
        raise ValueError(f"template {template_id} not found")
    try:
        rendered = render(tpl.body, params or {})
        commands = extract_commands(rendered)
    except TemplateError_ as exc:
        raise ValueError(f"template render failed: {exc}") from exc

    return ConfigPlan(
        plan_id=_new_plan_id(),
        vendor=tpl.vendor or detect_vendor(commands),
        device_ids=list(device_ids),
        commands=commands,
        source=PlanSource(source="template", template_id=template_id),
        risk=empty_risk(),
        meta={"template_name": tpl.name, "params": dict(params or {})},
    )


def build_plan_from_script(
    db: Session,
    *,
    script_id: int,
    device_ids: list[int],
) -> ConfigPlan:
    from app.services.script_parser import parse_script_to_plan, ScriptParserError
    from app.models.ai import AIScript

    script = db.get(AIScript, script_id)
    if script is None:
        raise ValueError(f"script {script_id} not found")

    try:
        extracted = parse_script_to_plan(script.source_code or "")
    except ScriptParserError as exc:
        raise ValueError(f"script rejected: {exc}") from exc

    return ConfigPlan(
        plan_id=_new_plan_id(),
        vendor=extracted.vendor or detect_vendor(extracted.commands),
        device_ids=list(device_ids),
        commands=list(extracted.commands),
        source=PlanSource(source="ai_script", script_id=script_id),
        risk=empty_risk(),
        meta={
            "parser_warnings": list(extracted.warnings),
            "requires_review": extracted.requires_review,
            "extraction_notes": list(extracted.notes),
            "script_name": script.name,
        },
    )


def assess_plan_risk(
    db: Session, plan: ConfigPlan, *, use_llm: bool = False,
) -> PlanRisk:
    rule = _rule_based_risk(plan.commands)
    plan_risk = PlanRisk(
        level="dangerous" if rule.is_dangerous else "safe",
        matched=list(rule.matched_patterns),
        requires_approval=rule.require_approval,
        ai_comment=None,
        blast_radius={},
    )

    if use_llm and rule.is_dangerous:
        try:
            from app.services.ai_risk import assess_job_risk
            full = assess_job_risk(
                db,
                commands=plan.commands,
                device_ids=plan.device_ids,
                use_llm=True,
            )
            plan_risk.ai_comment = full.get("ai_comment")
            plan_risk.blast_radius = full.get("blast_radius", {}) or {}
        except Exception as exc:
            logger.warning("plan risk LLM augment failed: {}", exc)

    plan.risk = plan_risk
    return plan_risk
