"""Command safety validator for network device configuration changes."""
from __future__ import annotations

import re
from typing import Optional

# High-risk keywords requiring manual approval
_DANGEROUS_PATTERNS = [
    r"\bshutdown\b(?!\s+vlan)",  # shutdown (except "shutdown vlan")
    r"\bno\s+ip\s+route\b",
    r"\bno\s+interface\b",
    r"\breload\b",
    r"\breboot\b",
    r"\bwrite\s+erase\b",
    r"\bformat\b",
    r"\bdelete\s+/recursive\b",
    r"\bundo\s+interface\b",
    r"\bundo\s+ip\s+route-static\b",
    r"\bno\s+vlan\b",
    r"\bundo\s+vlan\b",
    r"\bclear\s+configuration\b",
    r"\breset\s+saved-configuration\b",
]

_DANGER_RE = re.compile("|".join(_DANGEROUS_PATTERNS), re.IGNORECASE)


class CommandRisk:
    """Command risk assessment result."""
    is_dangerous: bool
    matched_patterns: list[str]
    require_approval: bool

    def __init__(self, is_dangerous: bool = False, matched_patterns: list[str] | None = None):
        self.is_dangerous = is_dangerous
        self.matched_patterns = matched_patterns or []
        self.require_approval = is_dangerous


def assess_commands(commands: list[str]) -> CommandRisk:
    """Scan commands for high-risk operations.

    Returns:
        CommandRisk with is_dangerous=True if any dangerous pattern matched.
    """
    matched = []
    for cmd in commands:
        if _DANGER_RE.search(cmd):
            matched.append(cmd)

    return CommandRisk(
        is_dangerous=len(matched) > 0,
        matched_patterns=matched,
    )


def format_risk_warning(risk: CommandRisk) -> str:
    """Format human-readable risk warning."""
    if not risk.is_dangerous:
        return ""

    lines = ["⚠️  HIGH-RISK COMMANDS DETECTED:"]
    for cmd in risk.matched_patterns[:5]:  # Show first 5
        lines.append(f"  • {cmd}")
    if len(risk.matched_patterns) > 5:
        lines.append(f"  ... and {len(risk.matched_patterns) - 5} more")
    lines.append("\n🛡️  Manual approval required before execution.")
    return "\n".join(lines)
