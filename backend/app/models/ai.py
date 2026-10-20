"""AI Agent data models.

Stores configuration (API key, base URL, model, system prompt), the rolling
chat history per user/device, audit log of every tool invocation the agent
performs, and the user-uploaded Python scripts the agent can analyze and run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _utcnow() -> callable:
    """Return current UTC time with timezone info (Python 3.12+ safe)."""
    return lambda: datetime.now(timezone.utc)


class AIConfig(Base):
    """Global AI provider settings.

    Single-row table (we always fetch id=1).  Encrypted secret fields are
    persisted via Fernet (handled in the service layer).
    """

    __tablename__ = "ai_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(32), default="openai")
    base_url: Mapped[str] = mapped_column(String(512), default="https://api.openai.com/v1")
    api_key_enc: Mapped[Optional[str]] = mapped_column(Text, default=None)
    model: Mapped[str] = mapped_column(String(64), default="gpt-4o-mini")
    temperature: Mapped[float] = mapped_column(default=0.3)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=False)
    # Persona — current tab the agent is focused on (chat | agent | monitor)
    active_persona: Mapped[str] = mapped_column(String(16), default="chat")
    # Last test status
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(default=None)
    last_test_message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # Monotonic counters for the dashboard stat cards
    total_chats: Mapped[int] = mapped_column(Integer, default=0)
    total_agent_actions: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Feature flags
    scripts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    multimodal_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stream_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    script_exec_timeout: Mapped[int] = mapped_column(Integer, default=30)
    script_max_output_kb: Mapped[int] = mapped_column(Integer, default=512)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), onupdate=_utcnow())
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), default=None)


class AIChatMessage(Base):
    """One turn in a chat session.

    Sessions are identified by (user_id, session_id, scope) so a user can
    have separate chats with "global chat", "device 5 debug", etc.

    `attachments` holds user-uploaded artifacts for THIS turn (URLs, file
    metadata, image references, MCP server configs, skill manifests, etc).
    Format is a list of dicts, e.g.
        [{"type": "image", "url": "data:image/png;base64,..."},
         {"type": "file",  "name": "topo.txt", "text": "..."},
         {"type": "url",   "value": "https://..."}]
    """

    __tablename__ = "ai_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(16), default="chat")  # chat | agent | monitor
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # system | user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    name: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    attachments: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), index=True)


class AIAgentLog(Base):
    """Audit trail of every tool call the agent invoked."""

    __tablename__ = "ai_agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    username: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(64), index=True)
    args: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    result_preview: Mapped[Optional[str]] = mapped_column(Text, default=None)
    ok: Mapped[bool] = mapped_column(default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), index=True)


class AIScript(Base):
    """User-uploaded Python script the agent can analyze + execute.

    The agent uses these for any workflow the user wants automated:
    bulk SSH, log parsing, compliance scans, custom diagnostics, etc.
    `source_code` is the literal text of the file.  `description` is a
    free-form human description; the agent sees it during tool listing so
    it can pick the right script.  `inputs_schema` documents expected
    kwargs (free-form JSON schema, surfaced to the LLM in tool defs).
    `last_run_*` fields give the LLM a quick "did this run successfully
    last time" signal.
    """

    __tablename__ = "ai_scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    source_code: Mapped[str] = mapped_column(Text, default="")
    inputs_schema: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    tags: Mapped[Optional[str]] = mapped_column(String(255), default=None)  # comma-separated
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Run telemetry (rolled forward on every execution)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    last_run_ok: Mapped[Optional[bool]] = mapped_column(default=None)
    last_run_message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    total_run_seconds: Mapped[float] = mapped_column(default=0.0)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow(), onupdate=_utcnow
    )


class AIScriptRun(Base):
    """Persistent record of a single execution of an AI script.

    The stdout/stderr captures are capped (size_bytes field) so we don't
    blow up the DB if a script prints megabytes.
    """

    __tablename__ = "ai_script_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("ai_scripts.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    invoked_by: Mapped[str] = mapped_column(String(64), default="system")
    args: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | failed | timeout
    return_code: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    stdout: Mapped[Optional[str]] = mapped_column(Text, default=None)
    stderr: Mapped[Optional[str]] = mapped_column(Text, default=None)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)


# ── Memory (long-running agent context) ───────────────────────────────────────
#
# Two-tier memory so the agent can follow cross-turn references like
# "这台设备的邻居" without the user having to repeat the device name.
#
# * ``AIConversationMemory`` is short-term: scoped to a single
#   (user_id, session_id, key).  Used to remember the device the user
#   last asked about, the job they were inspecting, etc.  Auto-expires
#   after 7 days of inactivity (a periodic cleanup task — or a fresh
#   read on each recall — clears stale rows).
# * ``AIUserPreference`` is long-term: one row per user.  Used to
#   remember which vendors/groups the user cares about, custom
#   prompt fragments, alert keywords, etc.  No expiry.


class AIConversationMemory(Base):
    """Short-term, session-scoped memory of a single (key, value) fact.

    Examples of keys:
      * ``last_device_id``       — {"device_id": 42, "name": "Core-SW1", "vendor": "huawei_vrp"}
      * ``last_job_id``          — {"job_id": 88, "kind": "template_apply"}
      * ``last_drift_id``        — {"drift_id": 17, "device_id": 42}
      * ``last_document_id``     — {"doc_id": 5, "title": "Huawei VRP 配置指南"}

    Conflict resolution is handled by the service layer: when a tool
    result updates an existing key with a different value, the service
    decides whether to overwrite silently or surface a confirmation
    request to the user (depending on whether ``key`` is in
    ``_CRITICAL_KEYS``).
    """

    __tablename__ = "ai_conversation_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(16), default="chat")  # chat | agent | monitor
    memory_type: Mapped[str] = mapped_column(String(32), default="device_context")
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="tool")  # tool | user | inference
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), onupdate=_utcnow())


class AIUserPreference(Base):
    """Long-term, per-user preferences that persist across sessions.

    One row per user (enforced by the unique=True constraint on
    ``user_id``).  Reads/writes are upserts via the service layer.
    """

    __tablename__ = "ai_user_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True,
    )
    default_group: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    preferred_vendors: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    alert_keywords: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    custom_prompts: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), onupdate=_utcnow())


# ── AI telemetry / cost monitoring ─────────────────────────────────
#
# Every LLM call (chat completions, HyDE) writes one row so we can
# build a cost dashboard and detect regressions.  Kept simple: one
# row per HTTP request, prompt/completion tokens for cost math,
# latency for SLA, kind to distinguish chat vs HyDE vs future
# streaming.

class AITelemetryLog(Base):
    """One row per LLM HTTP request for cost + latency analysis."""

    __tablename__ = "ai_telemetry_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True,
    )
    session_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    scope: Mapped[str] = mapped_column(String(16), default="chat")
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat | hyde | test | script
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), index=True)
