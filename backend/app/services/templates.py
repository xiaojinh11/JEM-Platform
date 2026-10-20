"""Jinja2-based template rendering."""
from __future__ import annotations

import re
from typing import Any, Iterable

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment


class TemplateError_(Exception):
    pass


_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
_ENV.filters["csv_join"] = lambda items, sep=",": sep.join(str(x) for x in items if x is not None)


def render(template: str, params: dict[str, Any]) -> str:
    try:
        tpl = _ENV.from_string(template)
        return tpl.render(**params)
    except TemplateError as exc:
        raise TemplateError_(str(exc)) from exc


def validate(template: str, sample_params: dict[str, Any]) -> dict[str, Any]:
    """Render template with sample params to catch missing variables."""
    errors: list[str] = []
    rendered = ""
    try:
        ast = _ENV.parse(template)
        from jinja2 import meta
        undeclared = meta.find_undeclared_variables(ast)
        missing = [v for v in undeclared if v not in sample_params]
        if missing:
            errors.append(f"missing variables: {sorted(missing)}")
        rendered = _ENV.from_string(template).render(**sample_params)
    except TemplateError as exc:
        errors.append(str(exc))
    return {"errors": errors, "rendered": rendered}


def extract_commands(rendered: str) -> list[str]:
    """Split rendered text into per-line commands."""
    lines = []
    for raw in rendered.splitlines():
        line = raw.rstrip()
        if not line or line.strip().startswith("#"):
            continue
        lines.append(line)
    return lines


_CLASS_C_RE = re.compile(r"^\s*(\w[\w-]*)#\s*$")


def is_prompt(line: str) -> bool:
    return bool(_CLASS_C_RE.match(line))
