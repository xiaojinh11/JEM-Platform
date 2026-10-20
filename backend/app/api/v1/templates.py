"""Template CRUD + preview."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.models.template import Template
from app.services.templates import extract_commands, render, validate, TemplateError_
from app.utils.command_validator import assess_commands, format_risk_warning

router = APIRouter()


class TemplateIn(BaseModel):
    name: str
    description: str | None = None
    body: str
    params_schema: str | None = None
    vendor: str | None = None


class TemplateOut(BaseModel):
    id: int
    name: str
    description: str | None
    body: str
    params_schema: str | None
    vendor: str | None
    created_by: str

    class Config:
        from_attributes = True


class ValidateResult(BaseModel):
    errors: list[str]
    rendered: str
    commands: list[str]
    risk_level: str = "safe"
    risk_message: str | None = None
    require_approval: bool = False


@router.get("", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """List all templates."""
    result = db.execute(select(Template).order_by(Template.name))
    templates = result.scalars().all()
    return templates


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Get a template by ID."""
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.post("", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Create a new template."""
    existing = db.execute(select(Template).where(Template.name == payload.name)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Template with this name already exists")

    template = Template(
        name=payload.name,
        description=payload.description,
        body=payload.body,
        params_schema=payload.params_schema,
        vendor=payload.vendor,
        created_by=user.username,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(template_id: int, payload: TemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Update an existing template."""
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    existing = db.execute(select(Template).where(Template.name == payload.name, Template.id != template_id)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Template with this name already exists")

    template.name = payload.name
    template.description = payload.description
    template.body = payload.body
    template.params_schema = payload.params_schema
    template.vendor = payload.vendor

    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Delete a template."""
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    db.delete(template)
    db.commit()


@router.post("/preview", response_model=ValidateResult)
def preview(params: dict, template_body: str):
    """Preview template rendering and assess risk."""
    sample = {k: v for k, v in params.items() if v is not None}
    errors = []
    rendered = ""
    commands = []
    risk_level = "safe"
    risk_message = None
    require_approval = False

    try:
        rendered = render(template_body, sample)
        commands = extract_commands(rendered)

        risk = assess_commands(commands)
        if risk.is_dangerous:
            risk_level = "dangerous"
            risk_message = format_risk_warning(risk)
            require_approval = True

    except TemplateError_ as e:
        errors.append(str(e))

    return ValidateResult(
        errors=errors,
        rendered=rendered,
        commands=commands,
        risk_level=risk_level,
        risk_message=risk_message,
        require_approval=require_approval,
    )
