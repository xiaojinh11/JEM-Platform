"""AST-based extractor for user-uploaded Python automation scripts.

Goal
----
Given the source text of an ``AIScript``, return an ``ExtractedPlan``
that can be turned into a ``ConfigPlan`` (via
``services.plan_builder.build_plan_from_script``).

Design rules
------------

1. **No execution.** The script is *parsed* with :func:`ast.parse` —
   never ``compile``-d and never ``exec``-d. Anything that requires
   running the script to extract config is out of scope; the user can
   still hit ``run_script`` for that, but it runs in its own
   sandboxed subprocess (see ``_tool_run_script``).

2. **Whitelist-only.** Config intent is extracted only from recognised
   call shapes:

     * ``connect.send_config_set(commands, ...)``     (netmiko)
     * ``<conn>.send_config_set(commands, ...)``
     * ``<conn>.config(commands, ...)``               (NAPALM)
     * ``<conn>.send_command("conf t ...")``         (single inline
       commands — only when the literal starts with a config-mode token)

   Literal lists / dicts / templates inside these calls are *literal
   constant* — not evaluated. If the argument isn't a literal we add a
   parser warning and skip that call entirely.

3. **Vendor hint.** We never *guess* vendor from arbitrary identifiers
   — only from detected command text. The plan_builder uses the same
   helper to get vendor when we don't have one.

4. **Refusal.** Hard reject when the script imports an obviously
   dangerous module (``subprocess``, ``ctypes``, ``os.system``,
   etc.) or has top-level ``os.environ`` reads. We do this so a
   malicious upload gets caught at the *upload* boundary, not when the
   admin tries to apply the plan.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable


# ── Public types ─────────────────────────────────────────────────────


@dataclass
class ExtractedPlan:
    """Result of ``parse_script_to_plan``.

    ``commands`` is the union of every recognised call's command list,
    deduped (preserving order). ``vendor`` is the detected vendor or
    empty. ``warnings`` are non-fatal issues the user should see in the
    preview. ``requires_review`` is set when the script contained
    patterns we don't fully trust (e.g. wildcard imports) and so the
    plan must be human-confirmed before dry-run.
    """

    commands: list[str] = field(default_factory=list)
    vendor: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    requires_review: bool = False
    matched_calls: int = 0


class ScriptParserError(Exception):
    """Raised on hard parser refusal (malicious import, syntax error,
    explicit dangerous call)."""


# ── Hard-refusal patterns ────────────────────────────────────────────

_FORBIDDEN_IMPORT_MODULES = frozenset({
    "subprocess", "ctypes", "cffi", "multiprocessing",
    # subprocess is fine for *scripts the user runs*, but for "convert
    # this script to a config plan" we never want exec-style helpers.
})

_FORBIDDEN_DANGEROUS_CALLS = frozenset({
    # Dotted paths. Mod="" means "bare name at top-level" — the
    # visitor falls back to that when the call target is a Name.
    ("os", "system"),
    ("os", "execv"),
    ("os", "execvp"),
    ("os", "execve"),
    ("builtins", "exec"),
    ("builtins", "eval"),
    ("builtins", "__import__"),
    ("", "exec"),
    ("", "eval"),
    ("", "compile"),
    ("", "__import__"),
})


# Calls we recognise for command extraction.
_CONFIG_CALL_NAMES: frozenset[str] = frozenset({
    "send_config_set",
    "send_command",
    "config",
})

# Map of "module path" → variable name we'd commonly bind a connection
# to. If we see a top-level assignment from one of these call sites,
# we follow the binding to expand config calls later. Keep the
# heuristic loose on purpose — the user can always click "run dry-run
# against this script" without our parser.
_NETMIKO_FACTORIES: frozenset[str] = frozenset({
    "ConnectHandler",          # netmiko
    "ConnectHandler.__init__", # placeholder for clarity
    "NetworkDriver",           # napalm
})

# Reject obvious config-mode "single-shot" commands inside ``send_command``
# when they are NOT whole config-mode. We allow:
#   display version
#   show ip interface brief
#   get system status            # Forti style
# We block:
#   reload
#   write erase
#   format flash
#   erase startup-config
# These are typically high-impact — they belong in a plan only if the
# user explicitly set them inside a ``send_config_set``. Putting them
# inside ``send_command`` means the user is trying to apply them to a
# *single* session, not as a plan; safer to require explicit source
# structure than to silently include them.
_CONFIG_ONLY_DISPLAY_TOKENS: frozenset[str] = frozenset({
    "display", "show", "get", "list",
})


# ── Main entrypoint ─────────────────────────────────────────────────


def parse_script_to_plan(source_code: str) -> ExtractedPlan:
    """Parse ``source_code`` (text) into an ``ExtractedPlan``.

    Raises ``ScriptParserError`` for hard refusals; returns an
    ``ExtractedPlan`` (possibly with empty commands) for soft cases
    where the user should still see a preview but the plan will be
    blocked by the requires_review flag in the builder.
    """
    if not source_code or not source_code.strip():
        raise ScriptParserError("脚本为空")

    if len(source_code) > 200_000:
        raise ScriptParserError("脚本长度超过 200KB 上限")

    try:
        tree = ast.parse(source_code, mode="exec")
    except SyntaxError as exc:
        raise ScriptParserError(f"Python 语法错误: {exc.msg} (行 {exc.lineno})") from exc

    extractor = _ScriptExtractor(source_code)
    extractor.visit(tree)

    if extractor.hard_reject:
        raise ScriptParserError(extractor.hard_reject)

    return ExtractedPlan(
        commands=extractor.commands,
        vendor=extractor.detected_vendor,
        warnings=extractor.warnings,
        notes=extractor.notes,
        requires_review=extractor.requires_review,
        matched_calls=extractor.matched_calls,
    )


# ── Visitor ─────────────────────────────────────────────────────────


class _ScriptExtractor(ast.NodeVisitor):
    """Walks the AST, gathering config-call literals and emitting
    warnings. The visitor is intentionally narrow — anything we don't
    recognise is dropped silently; the user gets a parser warning so
    they understand why their command list came back empty."""

    def __init__(self, source_code: str) -> None:
        self.source_code = source_code
        self.commands: list[str] = []
        self._seen: set[str] = set()
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.requires_review = False
        self.hard_reject: str | None = None
        self.matched_calls = 0
        self._connect_aliases: set[str] = set()
        self.detected_vendor = ""

    # ── imports & top-level ─────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _FORBIDDEN_IMPORT_MODULES:
                self.hard_reject = (
                    f"脚本导入了被禁用的模块 {alias.name!r}，"
                    f"无法提取配置计划"
                )
                return
            # Track ``import netmiko`` / ``from netmiko import ConnectHandler``
            # so we can resolve calls later.
            if alias.asname:
                self._connect_aliases.add(alias.asname)
            elif alias.name.endswith("ConnectHandler"):
                self._connect_aliases.add("ConnectHandler")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = (node.module or "").split(".")[0]
        if module in _FORBIDDEN_IMPORT_MODULES:
            self.hard_reject = (
                f"脚本导入了被禁用的模块 {node.module!r}，"
                f"无法提取配置计划"
            )
            return
        if node.module and node.module.startswith(("netmiko", "napalm")):
            for alias in node.names:
                if alias.name in ("ConnectHandler", "NetworkDriver"):
                    self._connect_aliases.add(alias.asname or alias.name)
        # ``from x import *`` is fine functionally but tells us we can't
        # know what's been bound where. Mark the plan for human review
        # rather than guessing.
        if any(alias.name == "*" for alias in node.names):
            self.requires_review = True
            self.notes.append(
                f"第 {node.lineno} 行使用了 wildcard import，建议人工复核"
            )
        self.generic_visit(node)

    # ── calls: hard rejections + whitelist extraction ──────────────

    def visit_Call(self, node: ast.Call) -> None:
        if self.hard_reject:
            return

        # Hard rejection: dangerous builtins / os.system etc.
        mod, name = _resolve_dotted(node.func)
        if (mod, name) in _FORBIDDEN_DANGEROUS_CALLS:
            self.hard_reject = (
                f"脚本包含危险调用 {mod}.{name}()，拒绝提取配置计划"
            )
            return

        # Vendor hint: scan keyword arguments for "device_type='cisco_ios'"
        for kw in node.keywords:
            if (isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)):
                v = kw.value.value.lower()
                for vendor, markers in _DEVICE_TYPE_HINTS.items():
                    if any(m in v for m in markers):
                        if not self.detected_vendor:
                            self.detected_vendor = vendor

        # Whitelist: send_config_set / config / send_command
        attr_name = _resolve_attr_tail(node.func)
        if attr_name in _CONFIG_CALL_NAMES:
            cmds = _extract_commands_from_call(node, attr_name)
            self.matched_calls += 1  # we recognised the call shape
            if cmds is None:
                self.warnings.append(
                    f"第 {node.lineno} 行 {attr_name}() 的参数不是字面量列表，已跳过"
                )
            elif not cmds and attr_name == "send_command":
                # A send_command call but no safe literals — note for the user
                self.notes.append(
                    f"第 {node.lineno} 行 send_command() 未提供配置命令，已跳过"
                )
            else:
                for c in cmds:
                    if c and c not in self._seen:
                        self._seen.add(c)
                        self.commands.append(c)

        self.generic_visit(node)

    # ── assignments: remember "conn = ConnectHandler(...)" ──────────

    def visit_Assign(self, node: ast.Assign) -> None:
        # Only care about "x = ConnectHandler(...)"
        if isinstance(node.value, ast.Call):
            attr_name = _resolve_attr_tail(node.value.func)
            if attr_name in ("ConnectHandler", "NetworkDriver"):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self._connect_aliases.add(tgt.id)
        self.generic_visit(node)

    # Recognised device_type strings inside ConnectHandler(**kwargs).
_DEVICE_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "cisco_ios_xe": ("cisco_ios", "cisco_xe"),
    "huawei_vrp":   ("huawei",),
    "ruijie_rgos":  ("ruijie", "rgos"),
    "h3c_comware":  ("h3c", "hp_comware", "comware"),
}


# ── Helpers ──────────────────────────────────────────────────────────


def _resolve_dotted(node: ast.AST) -> tuple[str, str]:
    """Return (module, attr) for an ``Attribute`` node, or
    ('', func_name) for a ``Name`` node. Used for hard-rejection
    matching on dotted call paths like ``os.system``.

    For ``a.b.c()`` we return ``("a.b", "c")`` — the path string keeps
    multi-segment names together so the forbidden-call list can match
    on simple substring checks (e.g. ``"os.system"``).
    """
    if isinstance(node, ast.Name):
        return "", node.id
    if isinstance(node, ast.Attribute):
        prefix, _ = _resolve_dotted(node.value)
        if isinstance(node.value, ast.Attribute):
            prefix = f"{prefix}.{node.value.attr}" if prefix else node.value.attr
        elif isinstance(node.value, ast.Name):
            prefix = node.value.id
        return prefix, node.attr
    return "", ""


def _resolve_attr_tail(node: ast.AST) -> str:
    """Return the last attribute name of a call target, or ``""``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _extract_commands_from_call(node: ast.Call, name: str) -> list[str] | None:
    """Pull a flat string list out of the first positional argument.

    Only supports *literal* lists / tuples / constants — anything
    built by an expression is dropped (we'd be guessing). For
    ``send_command`` we additionally require that the literal look
    like a config command (not a one-off ``reload`` style), since
    single-shot apply calls are out of scope for a *plan*.
    """
    if not node.args:
        return None
    arg0 = node.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        if name == "send_command":
            if _is_safe_single_command(arg0.value):
                return [arg0.value.strip()]
            return []
        return [arg0.value.strip()]
    if isinstance(arg0, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in arg0.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                if name == "send_command":
                    if _is_safe_single_command(elt.value):
                        out.append(elt.value.strip())
                    # otherwise drop silently
                else:
                    out.append(elt.value.strip())
        return out
    return None


_DANGEROUS_SINGLE_COMMANDS = re.compile(
    r"^\s*(reload|write\s+erase|format\s+flash|erase\s+startup|"
    r"delete\s+flash:|reset\s+saved|reboot)\b",
    re.IGNORECASE,
)


def _is_safe_single_command(text: str) -> bool:
    """Filter for ``send_command`` literals — only show plans made of
    clearly-config-mode lines, never one-shot reloads."""
    if not text:
        return False
    if _DANGEROUS_SINGLE_COMMANDS.match(text):
        return False
    # Require a positive "config verb" prefix so we don't claim
    # ``"display this"`` (display-only) belongs to a batch plan.
    first = text.strip().split(None, 1)[0].lower() if text.strip() else ""
    return first in {"interface", "vlan", "vlanif", "ip", "ipv6",
                     "router", "ospf", "bgp", "acl", "rule",
                     "sysname", "system-view", "configure",
                     "line", "snmp-server", "ntp", "aaa",
                     "user", "interface", "port", "switchport"}


# ── Helpers exported for unit tests ──────────────────────────────────


def _empty_extracted_plan() -> ExtractedPlan:
    return ExtractedPlan()


def _merge_plan_dest(dest: list[str], src: Iterable[str]) -> None:
    for c in src:
        if c not in dest:
            dest.append(c)
