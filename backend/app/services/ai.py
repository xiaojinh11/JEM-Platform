"""AI Agent service.

High-level orchestration: chat sessions, LLM calls, tool execution loop,
history management, and the config / connection-test public API.
Tool implementations live in ``ai_tools.py`` and ``ai_tool_impl.py``.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.ai import AIConfig, AIChatMessage, AIAgentLog
from app.models.device import Device
from app.services.ai_tools import TOOL_DEFS
from app.services.ai_tool_impl import execute_tool
from app.services.ai_react import (
    ReActController,
    ReActStep,
    run_rag_fallback,
)
from app.services.ai_telemetry import record_llm_call

MAX_HISTORY = 20
MAX_AGENT_ROUNDS = 3
MAX_RETRIES = 2
RETRY_CODES = {502, 503, 504}


# ── Lightweight LLM call (no tools, no streaming) ──────────────────
# Used by RAG HyDE — we just need a short text completion, no need to
# re-implement the chat() orchestration.  Returns the assistant text or
# ``None`` on any failure (caller falls back to plain BM25).


async def _call_llm_plain(
    cfg: "AIConfig",
    messages: list[dict],
    *,
    max_tokens: int = 220,
    temperature: float = 0.4,
) -> str | None:
    """Single-shot LLM call without tools. Returns text or ``None``.

    Decrypts the API key from ``cfg.api_key_enc`` on demand.  Does NOT
    log the request body — only the model name and response status —
    so decrypted secrets never reach disk via DEBUG log lines.
    """
    try:
        api_key = decrypt_secret(cfg.api_key_enc) if cfg.api_key_enc else ""
    except Exception:  # noqa: BLE001
        return None
    if not api_key:
        return None

    t0 = time.time()
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": cfg.model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": messages,
                },
            )
        # We intentionally skip persisting HyDE rows to telemetry: the
        # call site (RAG) doesn't carry a SQLAlchemy session, and HyDE
        # is small (~150 tokens).  Cost impact is negligible; chat
        # calls cover the dominant share of usage.
        _duration_ms = int((time.time() - t0) * 1000)  # noqa: F841
        if resp.status_code >= 400:
            logger.warning("HyDE LLM call failed: status={}", resp.status_code)
            return None
        body = resp.json()
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content")
        return text.strip() if isinstance(text, str) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("HyDE LLM call exception: {}", exc)
        return None

# ── Response text cleaning ──────────────────────────────────────────────────────────

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u0085\u00a0]+", re.UNICODE)
_MULTI_BLANK = re.compile(r"[ \t]{3,}")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+|/[^)]*)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL = re.compile(r"(?<![*\w])(\*)(?!\*)([^*]+?)(?<![*\w])\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADER = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_HRULE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_NOISE_DASH = re.compile(r"-{3,}")
_NOISE_UNDER = re.compile(r"_{3,}")
_NOISE_DOTS = re.compile(r"\.{4,}")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_response(text: str) -> str:
    text = _CONTROL_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_BLANK.sub("  ", text)
    return text.strip() + "\n"


def clean_for_tool(text: str, max_chars: int = 400) -> str:
    """Sanitise tool-result snippets before they go back to the LLM."""
    if not text:
        return ""
    text = _CONTROL_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITAL.sub(r"\2", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_HRULE.sub("", text)
    text = _NOISE_DASH.sub(" — ", text)
    text = _NOISE_UNDER.sub(" ", text)
    text = _NOISE_DOTS.sub("…", text)
    text = _MULTI_BLANK.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " …"
    return text


# ── Multimodal content builder ─────────────────────────────────────────────────────

def _build_message_content(text: str, attachments: Optional[list], cfg: AIConfig) -> Any:
    if not attachments or not cfg.multimodal_enabled:
        return text
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    for att in attachments:
        att_type = att.get("type", "unknown")
        if att_type == "image" and "url" in att:
            blocks.append({"type": "image_url", "image_url": {"url": att["url"], "detail": "low"}})
        elif att_type == "file" and "text" in att:
            blocks.append({"type": "text", "text": f"[附件 {att.get('name','file')}]\n{att['text']}"})
        elif att_type == "url" and "value" in att:
            blocks.append({"type": "text", "text": f"[URL: {att['value']}]"})
        elif att_type in ("skill", "mcp"):
            blocks.append({"type": "text", "text": f"[{att_type.upper()}: {att.get('name','?')}]\n{json.dumps(att, ensure_ascii=False)}"})
    return blocks if blocks else text


# ── Config helpers ──────────────────────────────────────────────────────────────────

def get_config(db: Session) -> AIConfig:
    cfg = db.get(AIConfig, 1)
    if not cfg:
        cfg = AIConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def update_config(
    db: Session, *,
    provider=None, base_url=None, api_key=None,
    model=None, temperature=None, max_tokens=None, system_prompt=None,
    enabled=None, active_persona=None, username="system",
) -> AIConfig:
    cfg = get_config(db)
    if provider is not None:
        cfg.provider = provider
    if base_url is not None:
        cfg.base_url = base_url
    if api_key:
        cfg.api_key_enc = encrypt_secret(api_key)
    if model is not None:
        cfg.model = model
    if temperature is not None:
        cfg.temperature = float(temperature)
    if max_tokens is not None:
        cfg.max_tokens = int(max_tokens)
    if system_prompt is not None:
        cfg.system_prompt = system_prompt
    if enabled is not None:
        cfg.enabled = bool(enabled)
    if active_persona is not None:
        cfg.active_persona = active_persona
    cfg.updated_at = datetime.now(timezone.utc)
    cfg.updated_by = username
    db.commit()
    db.refresh(cfg)
    return cfg


def _decrypt_api_key(cfg: AIConfig) -> Optional[str]:
    if not cfg.api_key_enc:
        return None
    try:
        return decrypt_secret(cfg.api_key_enc)
    except (ValueError, Exception):
        return None


# ── System prompt factory ──────────────────────────────────────────────────────────

def _format_memory_block(facts: dict) -> str:
    """Render the short-term memory dict as a system-prompt reminder.

    Empty input → empty string; the caller skips the injection.
    Keys are translated to Chinese prompts that map to the tool/API
    the agent is most likely to use to resolve the reference.
    """
    if not facts:
        return ""

    lines = ["## 当前会话上下文（来自记忆）"]
    lines.append("用户最近几次对话里提到的关键事实，无需重复询问，直接使用：\n")

    for key, value in facts.items():
        if not value:
            continue
        if key == "last_device_id" and isinstance(value, dict):
            name = value.get("name") or f"id={value.get('device_id')}"
            lines.append(
                f"- 用户最近讨论的设备：**{name}**"
                + (f"（vendor={value.get('vendor')}）" if value.get("vendor") else "")
                + f" → device_id=`{value.get('device_id')}`"
            )
        elif key == "last_job_id" and isinstance(value, dict):
            jid = value.get("job_id")
            kind = value.get("kind") or "未知类型"
            lines.append(f"- 用户最近讨论的任务：job_id=`{jid}`，类型={kind}")
        elif key == "last_drift_id" and isinstance(value, dict):
            did = value.get("drift_id")
            dev = value.get("device_id")
            lines.append(
                f"- 用户最近讨论的漂移：drift_id=`{did}`"
                + (f"，device_id={dev}" if dev else "")
            )
        elif key == "last_document_id" and isinstance(value, dict):
            title = value.get("title") or ""
            lines.append(f"- 用户最近查阅的文档：《{title}》（doc_id=`{value.get('doc_id')}`）")
        elif key == "last_credential_id" and isinstance(value, dict):
            lines.append(f"- 用户最近使用的凭证：credential_id=`{value.get('credential_id')}`")

    lines.append('\n回答时**先确认**用户引用的是哪个事实（『它』『刚才那个』），如有歧义再追问。')
    return "\n".join(lines)


def _format_preferences_block(prefs: Optional[dict]) -> str:
    """Render long-term user preferences as a system-prompt reminder.

    Empty / unset preferences → empty string (caller skips).
    Only emits a block when at least one field has a usable value —
    keeps the prompt lean when the user hasn't customized anything.
    """
    if not prefs:
        return ""

    useful: list[str] = []

    if prefs.get("default_group"):
        useful.append(
            f"- 默认关注分组：`{prefs['default_group']}`"
            "（list_devices、drift scan 等未指定过滤时优先用此分组）"
        )

    vendors = prefs.get("preferred_vendors") or []
    if vendors and isinstance(vendors, list):
        useful.append(
            "- 常用 vendor 优先级：" + " > ".join(f"`{v}`" for v in vendors)
            + "（多厂商设备混杂时按此排序解读）"
        )

    keywords = prefs.get("alert_keywords") or []
    if keywords and isinstance(keywords, list):
        useful.append(
            "- 告警关键词（用户更关心这几类异常）："
            + "、".join(f"「{kw}」" for kw in keywords)
        )

    custom = prefs.get("custom_prompts") or {}
    if isinstance(custom, dict) and custom:
        # Each entry is a labeled extra instruction; render as bullet.
        custom_lines = []
        for label, text in custom.items():
            if isinstance(text, str) and text.strip():
                custom_lines.append(
                    f"  - **{label}**:`{text.strip()}`"
                )
        if custom_lines:
            useful.append("- 用户附加指令：" + "\n".join(custom_lines))

    if not useful:
        return ""

    out = ["## 用户长期偏好（来自偏好表）"]
    out.append("以下设置由用户自行配置，长期生效。每次回答都按此处理：\n")
    out.extend(useful)
    out.append("\n如有冲突，先按用户的『附加指令』处理，再用默认分组/优先级。")
    return "\n".join(out)


def _system_prompt(scope: str) -> str:
    base = (
        "你是一个专业的网络运维助手，名字叫 **NAW AI**（Network Automation Workbench）。\n"
        "你服务于数通工程师，负责网络设备的配置管理、故障诊断、自动化运维。\n\n"
        "## 输出格式规范（严格遵守）\n"
        "- 语言与用户一致（中文问中文答）\n"
        "- 用 **加粗标题** 分隔段落\n"
        "- 用 `- ` 或 `1.` 列出要点\n"
        "- 用 ```语言 包裹代码块\n"
        "- 关键词加 ** 加粗\n"
        "- 用 Markdown 表格展示结构化数据\n"
        "- 不要出现控制字符、奇怪的符号或乱码\n\n"
        "## 工具使用规则（Agent / Monitor 模式）\n"
        "1. 每次只调用 **1 个**工具，等结果返回后再决定下一步\n"
        "2. **同一个工具不要重复调用超过 2 次**——结果不会因为重复调用而改变\n"
        "3. 工具返回后，**直接回答用户**，不要继续调用更多工具\n"
        "4. 不知道就说不知道，不要猜测设备 IP 或配置\n"
        "5. 绝不捏造数据\n\n"
        "## 知识库工具使用技巧\n"
        "- **强制检索原则**：回答任何「配置」「命令」「手册」「怎么配」「XX 是什么意思」类问题时，"
        "**必须先调用检索工具**，拿到结果再回答。绝不跳过检索直接凭记忆回答。\n"
        "- **优先使用 rag_search**（多路召回 + HyDE）：\n"
        "  * 先用 LLM 生成假设文档 → 再 BM25 检索该假设 + 原 query → RRF 融合排序\n"
        "  * 召回率显著高于 search_documents，特别是口语化查询（『怎么排查邻居不通』『如何修 OSPF 邻居』）\n"
        "  * 传入 query 时**用用户原始句子**，不要预先关键词化（让 HyDE 处理）\n"
        "- **search_documents 是 BM25 单路召回**：仅在用户已经给出明确关键词（命令名/错误码/型号）时使用\n"
        "- **list_documents**：仅当用户问「有哪些文件」「手册库有什么」时使用，不需要关键词\n"
        "- **score 分档解读**（BM25 原始分数）：\n"
        "  * score ≥ 1.5 → 高置信度，直接引用回答\n"
        "  * score 0.5 ~ 1.5 → 中置信度，引用 + 注明「根据文档可能是……」\n"
        "  * score < 0.5 → 不要引用\n"
        "- **无召回拒答机制**：当工具返回 ``no_recall=True`` 或 count=0 时，**明确告知用户「知识库无相关内容」**，"
        "建议：换更具体的关键词（厂商名、命令关键字、错误码）/ 在「手册库」手动浏览 / 联系管理员补充文档。**绝不编造答案**。\n"
        "- **引用要求**：每个事实点必须附上来源，格式：`📖 《文档标题》`（来自 score ≥ 0.5 的命中）\n\n"
        "## 任务创建流程\n"
        "- 创建任务需要：任务名、类型（backup/batch_read/template_apply）、目标设备 ID 列表\n"
        "- 设备 ID 可从 list_devices 获取；模板 ID 可从 list_templates 获取\n"
        "- 创建后告诉用户任务已创建，需在「批量任务」页面审批并启动\n\n"
        "## 输出「可下发配置计划」时必须使用 propose_config_plan 工具\n"
        "- 当用户要求「帮我配 X」「生成 Y 配置命令」「下发 Z」等**实质性的配置变更**时，"
        "**必须**调用 `propose_config_plan(commands=[...], vendor=..., summary=...)` 工具输出计划，"
        "而不是把命令写到 Markdown 代码块里。\n"
        "- 命令必须是**可直接下发到设备的具体命令**（如 `vlan 100`、`interface GigabitEthernet0/0/1`），"
        "严禁用 `{{ variable }}` 模板占位符——占位符会被前置校验拒绝。\n"
        "- 调用 propose_config_plan 后，回复里**简要说明**该计划的作用，由前端负责渲染「Dry-Run 该 AI 计划」按钮，"
        "用户点击后会自动跳转到「批量任务」页面跑 dry-run 风险评估。\n"
        "- **不要**在 chat 里直接列命令后再说「请自行复制粘贴到设备」——这是普通文本回答的写法，"
        "在本系统里会绕过 dry-run 的高危命令检测和审批流。\n"
        "- 仅在用户**仅询问**「怎么配」「命令是什么」（不需要执行）时，"
        "才在 Markdown 代码块里展示示例配置。\n"
    )
    if scope == "agent":
        base += (
            "## Agent 模式\n"
            "你是 **Agent**，主动性强的分析师。\n"
            "遇到复杂任务时主动规划多步：先 list_devices，再 get_device_config，"
            "再 diff_revisions，综合分析后给出报告。\n"
            "你**不直接**修改设备，所有写入经审批流程进行。\n\n"
            "## 拓扑工具使用技巧\n"
            "- 用户问「网络多大」「核心多少台」「按厂商分布」「哪些设备」「几家厂商」「谁家最近飘得多」"
            "→ **topology_overview**（group_by 默认 role，可改 vendor / group_name / model）\n"
            "- 用户问「X 设备的邻居 / 关联 / 凭据 / 最近漂移」→ **get_device_neighbors**(device_id=X, hops=1)\n"
            "- 用户问「某条 drift 影响谁 / 同设备最近还飘过没」→ **get_affected_devices**(drift_id=N)\n"
            "- 用户问「我们有几台 Cisco 3850 / 所有 huawei 设备」→ **search_by_vendor_model**\n"
            "- **graph 类工具 + list_devices 的区别**：前者跨表关联一次性拿到答案，"
            "后者只是单表扫描。问「X 影响谁」优先用 graph，问「列设备」用 list_devices。\n\n"
            "## 协议 / 接口 工具使用技巧\n"
            "- 问「X 设备的接口、VLAN、IP、shutdown 状态」→ **get_interface_status**(device_id=X)\n"
            "- 问「BGP peer 列表 / 哪个 peer 没配密码 / AS 拓扑」→ **get_bgp_summary**(device_id=X)\n"
            "- 问「OSPF 进程 / 哪些网段在 OSPF / area 拓扑」→ **get_ospf_neighbors**(device_id=X)\n"
            "- 这三个工具都基于 *运行配置* 解析（不是实时操作数据），"
            "shutdown 表示 *配置上* shutdown，未必反映链路物理状态。\n"
        )
    elif scope == "monitor":
        base += (
            "## Monitor 模式\n"
            "你是 **Monitor**，专注实时告警和巡检。\n"
            "主动查看漂移事件，用 trigger_drift_scan 主动扫描，"
            "保持简洁、客观、数据优先。\n"
        )
    return base


# ── Chat (non-streaming) ──────────────────────────────────────────────────────────

def chat(db: Session, *, user_id: int, username: str, scope: str,
         session_id: str, user_message: str,
         attachments: Optional[list[dict]] = None) -> dict:
    from app.services import ai_runtime

    cfg = get_config(db)
    api_key = _decrypt_api_key(cfg)
    if not cfg.enabled or not api_key:
        return {"error": "AI Agent 未启用或未配置 API Key，请在「AI 配置」面板中启用。", "ok": False}

    # Session context — tools like ``propose_config_plan`` need to
    # know which chat they belong to so concurrent sessions on
    # different tabs don't trample each other's plan staging area.
    ai_runtime.set_session(user_id, session_id)
    sess_key = (user_id, session_id)
    try:
        return _chat_run(
            db, cfg=cfg, user_id=user_id, username=username,
            scope=scope, session_id=session_id,
            user_message=user_message, attachments=attachments,
        )
    finally:
        # Belt-and-suspenders: if anything in ``_chat_run`` raised
        # before reaching the in-function ``pop_plan_proposal``, the
        # proposal would otherwise sit in the in-memory dict and
        # leak into the user's next turn on this session. Always pop
        # once at request end — ``pop_plan_proposal`` is idempotent
        # (returns ``None`` if nothing was staged).
        try:
            from app.services import ai_tool_impl
            ai_tool_impl.pop_plan_proposal(sess_key)
        except Exception:  # noqa: BLE001
            pass
        ai_runtime.clear_session()


def _chat_run(
    db: Session, *, cfg: AIConfig, user_id: int, username: str, scope: str,
    session_id: str, user_message: str,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """Inner chat loop — split out so ``chat()`` can wrap it in the
    session-context ``try/finally``. Real work happens here."""
    db.add(AIChatMessage(
        user_id=user_id, username=username, scope=scope, session_id=session_id,
        role="user", content=user_message, attachments=attachments,
    ))
    db.commit()

    history = (
        db.query(AIChatMessage)
        .filter_by(user_id=user_id, scope=scope, session_id=session_id)
        .order_by(AIChatMessage.created_at.asc()).all()
    )
    if len(history) > MAX_HISTORY:
        for old in history[:-MAX_HISTORY]:
            db.delete(old)
        db.commit()
        history = history[-MAX_HISTORY:]

    # ── Inject short-term memory into the system prompt ──────────────
    # Read the session's stored facts (last_device_id, last_job_id, …)
    # and tack them onto the system prompt as a small reminder block.
    # The agent can then resolve cross-turn references like "它的邻居"
    # without the user having to repeat themselves.
    try:
        from app.services import ai_memory
        mem_facts = ai_memory.recall_all_memory(
            db, user_id=user_id, session_id=session_id, scope=scope,
        )
        mem_block = _format_memory_block(mem_facts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory recall failed for user {}: {}", user_id, exc)
        mem_block = ""

    messages = []
    system_context = _system_prompt(scope)
    if mem_block:
        system_context = f"{system_context}\n\n{mem_block}"

    # ── Inject long-term user preferences into the system prompt ────
    # Tries to render a small block.  Missing / unset prefs → empty
    # string, so unconfigured users see no extra noise in the prompt.
    try:
        from app.services import ai_memory as _ai_mem
        prefs_block = _format_preferences_block(
            _ai_mem.get_preferences(db, user_id=user_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("preferences fetch failed for user {}: {}", user_id, exc)
        prefs_block = ""
    if prefs_block:
        system_context = f"{system_context}\n\n{prefs_block}"
    for m in history:
        att = m.attachments
        raw_content = _build_message_content(m.content, att, cfg)
        msg: dict[str, Any] = {"role": m.role, "content": raw_content}
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.name:
            msg["name"] = m.name
        if system_context and msg["role"] == "user" and (isinstance(msg["content"], str) and msg["content"]):
            if isinstance(msg["content"], str):
                msg["content"] = f"{system_context}\n\n{msg['content']}"
            elif isinstance(msg["content"], list):
                for block in msg["content"]:
                    if block.get("type") == "text":
                        block["text"] = f"{system_context}\n\n{block['text']}"
                        break
            system_context = ""
        messages.append(msg)
    if system_context:
        messages.insert(0, {"role": "user", "content": system_context})

    final_text = ""
    total_tokens = 0
    used_tools = []
    rag_fallback_pending = False
    last_user_query = user_message or ""  # captured for the RAG fallback path
    react_controller = ReActController(
        same_tool_fail_threshold=int(getattr(settings, "react_same_tool_fail_threshold", 2)),
        empty_result_threshold=int(getattr(settings, "react_empty_result_threshold", 2)),
        enabled=bool(getattr(settings, "react_enabled", True)),
    )
    for round_no in range(MAX_AGENT_ROUNDS):
        req: dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "messages": messages,
        }
        if scope in ("agent", "monitor"):
            req["tools"] = TOOL_DEFS

        last_exc: Exception | None = None
        round_t0 = time.time()  # for telemetry duration
        for attempt in range(MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=180) as client:
                    resp = client.post(
                        f"{cfg.base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=req,
                    )
                logger.debug("LLM response: status={} body={}", resp.status_code, resp.text[:500])
                if resp.status_code in RETRY_CODES:
                    last_exc = Exception(f"上游服务暂时不可用 (HTTP {resp.status_code})")
                    if attempt < MAX_RETRIES:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    try:
                        record_llm_call(
                            db, user_id=user_id, session_id=session_id,
                            scope=scope, kind="chat", model=cfg.model,
                            duration_ms=int((time.time() - round_t0) * 1000),
                            ok=False, error=f"HTTP {resp.status_code}", fallback=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {
                        "error": (
                            f"LLM 上游服务暂时不可用（HTTP {resp.status_code}）。"
                            "这是第三方 API provider 的临时故障，请稍后再试。"
                        ),
                        "ok": False,
                    }
                if resp.status_code >= 400:
                    try:
                        err_body = resp.json()
                        upstream_msg = (
                            err_body.get("error", {}).get("message", "")
                            or err_body.get("message", "")
                            or err_body.get("error", "")
                        )
                    except Exception:
                        upstream_msg = ""
                    friendly = f"LLM 上游错误 {resp.status_code}" + (f": {upstream_msg}" if upstream_msg else "")
                    try:
                        record_llm_call(
                            db, user_id=user_id, session_id=session_id,
                            scope=scope, kind="chat", model=cfg.model,
                            duration_ms=int((time.time() - round_t0) * 1000),
                            ok=False, error=friendly[:200], fallback=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {"error": friendly, "ok": False}
                try:
                    data = resp.json()
                except Exception:
                    try:
                        record_llm_call(
                            db, user_id=user_id, session_id=session_id,
                            scope=scope, kind="chat", model=cfg.model,
                            duration_ms=int((time.time() - round_t0) * 1000),
                            ok=False, error="unparseable response", fallback=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {"error": f"上游返回非预期格式 (HTTP {resp.status_code})。请检查 Base URL 和 API Key。", "ok": False}
                break
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("LLM call failed after {} retries: {}", attempt + 1, exc)
                # Record failure to telemetry so we can spot provider flakiness.
                try:
                    record_llm_call(
                        db, user_id=user_id, session_id=session_id,
                        scope=scope, kind="chat", model=cfg.model,
                        duration_ms=int((time.time() - round_t0) * 1000),
                        ok=False, error=str(exc)[:200], fallback=False,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return {"error": f"LLM 调用失败: {exc}（已重试 {attempt + 1} 次）", "ok": False}
        else:
            return {"error": f"LLM 调用失败: {last_exc}", "ok": False}

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)
        choice = (data.get("choices") or [{}])[0]
        msg_obj = choice.get("message") or {}
        assistant_text = clean_response(msg_obj.get("content") or "")
        tool_calls = msg_obj.get("tool_calls") or []

        # Telemetry: persist one row per successful LLM HTTP call so we
        # can compute cost / latency dashboards.  Failures get a
        # separate row with ok=False.
        try:
            record_llm_call(
                db,
                user_id=user_id,
                session_id=session_id,
                scope=scope,
                kind="chat",
                model=cfg.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=int((time.time() - round_t0) * 1000),
                ok=True,
                fallback=False,
            )
        except Exception:  # noqa: BLE001
            pass

        a_msg = AIChatMessage(
            user_id=user_id, username=username, scope=scope, session_id=session_id,
            role="assistant", content=assistant_text,
            tool_calls=tool_calls if tool_calls else None,
        )
        db.add(a_msg)
        db.commit()
        db.refresh(a_msg)

        if not tool_calls or scope not in ("agent", "monitor"):
            final_text = assistant_text
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                raw_args = fn.get("arguments") or "{}"
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}

            t0 = time.time()
            result, ok, err = execute_tool(db, name, args, username)
            dur_ms = int((time.time() - t0) * 1000)

            db.add(AIChatMessage(
                user_id=user_id, username=username, scope=scope, session_id=session_id,
                role="tool", content=result, tool_call_id=tc.get("id"), name=name,
            ))
            db.add(AIAgentLog(
                user_id=user_id, username=username, session_id=session_id,
                tool=name, args=args, result_preview=result[:500], ok=ok, error=err,
                duration_ms=dur_ms,
            ))
            db.commit()

            # ── Auto-record successful tool outputs to short-term memory ──
            # Only write on success: an errored tool call shouldn't shape
            # what the agent "remembers" about the world.  Conflicts are
            # logged but not surfaced mid-turn — they'd interrupt a tool
            # loop; the next user turn gives a cleaner moment to ask.
            if ok:
                try:
                    from app.services import ai_memory
                    mem_res = ai_memory.record_tool_memory(
                        db,
                        user_id=user_id,
                        session_id=session_id,
                        scope=scope,
                        tool_name=name,
                        tool_args=args,
                        tool_result=result,
                    )
                    if mem_res is not None and mem_res.get("conflict"):
                        logger.info(
                            "memory conflict during tool {}: key={} new={}",
                            name, mem_res.get("key"), mem_res.get("new"),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("record_tool_memory failed: {}", exc)

            used_tools.append({"tool": name, "ok": ok, "ms": dur_ms})
            react_step = ReActStep(
                round=round_no + 1,
                tool=name,
                args=args,
                ok=ok,
                observation=result[:300] if ok else (err or "")[:300],
                duration_ms=dur_ms,
            )
            react_decision = react_controller.observe_round(react_step)

            messages.append({"role": "assistant", "content": assistant_text, "tool_calls": [tc]})
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": result})

            # ── Reflection: same tool failing in a row → fallback ──
            if react_decision.kind == "fallback_rag":
                logger.info(
                    "ReAct fallback triggered (round={} reason={})",
                    round_no + 1, react_decision.reason,
                )
                # Force one more round to give the LLM a chance to
                # acknowledge the failure and act on the hint.
                messages.append({
                    "role": "system",
                    "content": react_decision.hint,
                })
                # If the loop still doesn't produce a useful finish
                # after the reflection hint, the next outer iteration
                # will surface the fallback answer below.
                rag_fallback_pending = True
    else:
        final_text = (
            "抱歉，当前问题较为复杂，已达到最大分析轮次未能获得最终结论。"
            "建议拆分成多个简单问题，或去对应功能页面手动操作。"
        )

    # ── RAG fallback post-loop ─────────────────────────────────────
    # If reflection kicked a hint into messages but the LLM still
    # didn't produce a satisfactory answer — i.e. the loop exhausted
    # rounds while a fallback was pending — synthesise an answer
    # directly from the knowledge base so the user gets something
    # RAG fallback post-loop: the reflection hint we injected into
    # ``messages`` earlier (inside the loop) tells the LLM to use
    # rag_search itself on the next round — the loop will exit via
    # the natural ``break`` once the LLM produces a non-tool answer.
    # We don't trigger a separate rag_search call here because
    # ``chat()`` is sync and ``rag_search`` is async; that wiring
    # lands when chat() itself becomes async.

    cfg.total_chats = (cfg.total_chats or 0) + 1
    cfg.total_tokens = (cfg.total_tokens or 0) + total_tokens
    cfg.total_agent_actions = (cfg.total_agent_actions or 0) + len(used_tools)
    db.commit()

    # Include the ReAct trace in the response so the UI / tests can
    # surface it.
    reasoning_trace = react_controller.trace_summary() if used_tools else None

    # ── Plan proposal pickup ───────────────────────────────────────
    # If the LLM invoked ``propose_config_plan`` during this chat
    # round, attach the proposal to the response so the frontend can
    # render a "Dry-Run this AI plan" button. The proposal is popped
    # (not peeked) so the *next* chat round doesn't reuse it.
    #
    # Note: if any earlier step in this function raised (commit,
    # memory recall, telemetry), we'd already have left the function
    # before reaching this line — so the proposal would *stay* staged
    # in the in-memory dict until the session ends. To prevent stale
    # proposals from leaking into the user's next turn, we also drain
    # the proposal inside ``chat()``'s ``finally`` block (see above).
    from app.services import ai_tool_impl
    sess_key = (user_id, session_id)
    proposal = ai_tool_impl.pop_plan_proposal(sess_key)
    plan_payload: Optional[dict] = None
    if proposal and isinstance(proposal, dict) and proposal.get("commands"):
        plan_payload = {
            "commands": proposal.get("commands", []),
            "vendor": proposal.get("vendor", ""),
            "notes": proposal.get("notes", []),
            "summary": proposal.get("summary", ""),
            "source": "ai_chat",
            "session_id": session_id,
        }

    return {
        "ok": True,
        "reply": final_text,
        "tokens": total_tokens,
        "tools_used": used_tools,
        "reasoning_trace": reasoning_trace,
        "plan": plan_payload,
    }# ── Connection test ─────────────────────────────────────────────────────────────────

def _parse_models_from_response(cfg: AIConfig, resp: httpx.Response) -> tuple[list[str], str]:
    try:
        payload = resp.json()
        if "data" in payload:
            return [m["id"] for m in payload.get("data", []) if m.get("id")][:30], ""
        for key in ("models", "data"):
            if key in payload and isinstance(payload[key], list):
                return [m["id"] if isinstance(m, dict) else str(m) for m in payload[key]][:30], ""
        if isinstance(payload, dict):
            model_id = payload.get("id") or payload.get("model")
            if model_id:
                return [str(model_id)], ""
        return [str(payload)[:100]], ""
    except Exception:
        return [], ""


def test_connection(db: Session) -> dict:
    cfg = get_config(db)
    api_key = _decrypt_api_key(cfg)
    if not api_key:
        msg = "未配置 API Key"
        cfg.last_tested_at = datetime.now(timezone.utc)
        cfg.last_test_ok = False
        cfg.last_test_message = msg
        db.commit()
        return {"ok": False, "message": msg}
    base = cfg.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    model_ids = []
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{base}/v1/models", headers=headers)
        ok = resp.status_code < 400
        if ok:
            model_ids, _ = _parse_models_from_response(cfg, resp)
            msg = f"连通 OK，找到 {len(model_ids)} 个模型"
        else:
            msg = f"上游返回 {resp.status_code}"
        cfg.last_tested_at = datetime.now(timezone.utc)
        cfg.last_test_ok = ok
        cfg.last_test_message = msg
        db.commit()
        return {"ok": ok, "message": msg, "models": model_ids, "status_code": resp.status_code}
    except Exception as exc:
        msg = f"连接失败: {exc}"
        cfg.last_tested_at = datetime.now(timezone.utc)
        cfg.last_test_ok = False
        cfg.last_test_message = msg
        db.commit()
        return {"ok": False, "message": msg}


# ── History helpers ─────────────────────────────────────────────────────────────────

def list_history(db: Session, *, user_id: int, scope: str, session_id: str, limit: int = 100) -> list:
    return (
        db.query(AIChatMessage)
        .filter_by(user_id=user_id, scope=scope, session_id=session_id)
        .order_by(AIChatMessage.created_at.asc()).limit(limit).all()
    )


def list_sessions(db: Session, *, user_id: int, limit: int = 30) -> list:
    from sqlalchemy import func
    rows = (
        db.query(
            AIChatMessage.session_id, AIChatMessage.scope,
            func.max(AIChatMessage.created_at).label("last_at"),
            func.count(AIChatMessage.id).label("msg_count"),
            func.max(AIChatMessage.content).label("preview"),
        )
        .filter_by(user_id=user_id)
        .group_by(AIChatMessage.session_id, AIChatMessage.scope)
        .order_by(func.max(AIChatMessage.created_at).desc()).limit(limit).all()
    )
    return [
        {"session_id": r.session_id, "scope": r.scope, "last_at": r.last_at,
         "msg_count": r.msg_count, "preview": (r.preview or "")[:80]}
        for r in rows
    ]


def clear_history(db: Session, *, user_id: int, scope: str, session_id: str) -> int:
    n = db.query(AIChatMessage).filter_by(
        user_id=user_id, scope=scope, session_id=session_id,
    ).delete()
    db.commit()
    return n
