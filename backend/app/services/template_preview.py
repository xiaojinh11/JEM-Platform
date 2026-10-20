"""Template preview and validation with risk assessment."""
from __future__ import annotations

from pydantic import BaseModel

from app.utils.command_validator import assess_commands, format_risk_warning
from app.services.templates import render, extract_commands


class TemplatePreviewRequest(BaseModel):
    template_text: str
    params: dict


class TemplatePreviewResponse(BaseModel):
    rendered: str
    commands: list[str]
    command_count: int
    risk_level: str  # safe | warning | dangerous
    risk_message: str | None = None
    require_approval: bool


def preview_template(template: str, params: dict) -> TemplatePreviewResponse:
    """Render template and assess command risk."""
    rendered = render(template, params)
    commands = extract_commands(rendered)
    risk = assess_commands(commands)

    risk_level = "dangerous" if risk.is_dangerous else "safe"
    risk_message = format_risk_warning(risk) if risk.is_dangerous else None

    return TemplatePreviewResponse(
        rendered=rendered,
        commands=commands,
        command_count=len(commands),
        risk_level=risk_level,
        risk_message=risk_message,
        require_approval=risk.require_approval,
    )
