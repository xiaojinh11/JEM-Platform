"""AI memory service — short-term (session) and long-term (user) tiers.

This module sits between the chat loop and the database.  The intent is
to give the agent cross-turn context without bloating the prompt with
the entire conversation history.

Short-term memory (``AIConversationMemory``):
    Scoped to (user_id, session_id, scope).  Keys are dotted strings
    like ``"last_device_id"`` or ``"last_job_id"``; values are JSON
    blobs.  Used to follow references like "它的邻居有哪些" where "它"
    refers back to a device the user just asked about.

    Conflict policy: when ``update_memory`` is asked to overwrite a
    critical key (in ``_CRITICAL_KEYS``) with a different value, it
    returns ``{"conflict": True, ...}`` so the chat layer can surface
    a confirmation to the user instead of silently overwriting.

Long-term memory (``AIUserPreference``):
    One row per user.  Holds things like ``preferred_vendors`` so the
    agent can default to a user's frequent search filters, or
    ``custom_prompts`` for the user to inject extra system context.
    No expiry; writes are upserts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.ai import AIConversationMemory, AIUserPreference

logger = logging.getLogger(__name__)


# Keys that, when overwritten with a *different* value, must surface a
# confirmation prompt instead of silently replacing the prior value.
# These are the facts that, if changed mid-conversation, would silently
# flip the agent's context under the user.
_CRITICAL_KEYS = frozenset({
    "last_device_id",
    "last_job_id",
    "last_drift_id",
    "last_credential_id",
    "target_device_id",
})


# Short-term memory TTL: a session is considered "fresh" for 7 days.  The
# recall path filters rows older than this regardless of any background
# cleanup task.
_MEMORY_TTL = timedelta(days=7)


# ── Short-term memory (session-scoped) ────────────────────────────────────────


def recall_memory(
    db: Session, *, user_id: int, session_id: str, scope: str, key: str,
) -> Optional[dict]:
    """Fetch a single memory value.  Returns None if missing or expired."""
    cutoff = datetime.now(timezone.utc) - _MEMORY_TTL
    row = (
        db.query(AIConversationMemory)
        .filter_by(user_id=user_id, session_id=session_id, scope=scope, key=key)
        .filter(AIConversationMemory.updated_at >= cutoff)
        .first()
    )
    return row.value if row else None


def recall_all_memory(
    db: Session, *, user_id: int, session_id: str, scope: str,
) -> dict[str, dict]:
    """Fetch all memory values for a session as a {key: value} dict.

    Expired rows are excluded.  Used by the chat layer to inject a
    small "current context" block into the system prompt.
    """
    cutoff = datetime.now(timezone.utc) - _MEMORY_TTL
    rows = (
        db.query(AIConversationMemory)
        .filter_by(user_id=user_id, session_id=session_id, scope=scope)
        .filter(AIConversationMemory.updated_at >= cutoff)
        .all()
    )
    return {r.key: r.value for r in rows}


def update_memory(
    db: Session, *,
    user_id: int,
    session_id: str,
    scope: str,
    key: str,
    value: Any,
    memory_type: str = "device_context",
    source: str = "tool",
    force: bool = False,
) -> dict:
    """Upsert a memory entry.

    Returns:
        ``{"ok": True}`` on success.
        ``{"ok": True, "updated": True}`` on first insert vs update.
        ``{"conflict": True, "old": ..., "new": ...}`` if the key is
        critical and the value would change — caller should surface a
        confirmation to the user unless ``force=True``.

    The caller commits the session — we keep the service layer
    commit-agnostic so it composes naturally with the chat loop.
    """
    value_dict = value if isinstance(value, dict) else {"value": value}

    existing = (
        db.query(AIConversationMemory)
        .filter_by(user_id=user_id, session_id=session_id, scope=scope, key=key)
        .first()
    )

    if existing is not None and not force:
        # Conflict detection: critical keys with a *different* value
        # require explicit confirmation.
        if key in _CRITICAL_KEYS and existing.value != value_dict:
            logger.info(
                "memory conflict: user=%s session=%s key=%s old=%s new=%s",
                user_id, session_id, key, existing.value, value_dict,
            )
            return {
                "conflict": True,
                "old": existing.value,
                "new": value_dict,
                "key": key,
            }

    if existing is None:
        db.add(AIConversationMemory(
            user_id=user_id,
            session_id=session_id,
            scope=scope,
            key=key,
            value=value_dict,
            memory_type=memory_type,
            source=source,
        ))
        return {"ok": True, "updated": False}

    existing.value = value_dict
    existing.memory_type = memory_type
    existing.source = source
    return {"ok": True, "updated": True}


def delete_memory(
    db: Session, *, user_id: int, session_id: str, scope: str, key: str,
) -> bool:
    """Drop a single memory entry.  Returns True if a row was deleted."""
    n = (
        db.query(AIConversationMemory)
        .filter_by(user_id=user_id, session_id=session_id, scope=scope, key=key)
        .delete()
    )
    return bool(n)


def cleanup_expired_memory(db: Session) -> int:
    """Delete rows older than ``_MEMORY_TTL``.  Returns row count.

    Safe to call from a periodic background task; a missing cleanup
    doesn't break correctness because ``recall_memory`` already filters
    by ``updated_at >= cutoff``.
    """
    cutoff = datetime.now(timezone.utc) - _MEMORY_TTL
    n = (
        db.query(AIConversationMemory)
        .filter(AIConversationMemory.updated_at < cutoff)
        .delete()
    )
    db.commit()
    return int(n)


# ── Long-term memory (user preferences) ─────────────────────────────────────


def get_preferences(db: Session, *, user_id: int) -> Optional[dict]:
    row = db.get(AIUserPreference, user_id) or db.query(AIUserPreference).filter_by(user_id=user_id).first()
    if not row:
        return None
    return {
        "default_group": row.default_group,
        "preferred_vendors": row.preferred_vendors or [],
        "alert_keywords": row.alert_keywords or [],
        "custom_prompts": row.custom_prompts or {},
    }


def upsert_preferences(
    db: Session,
    *,
    user_id: int,
    default_group: Optional[str] = None,
    preferred_vendors: Optional[list] = None,
    alert_keywords: Optional[list] = None,
    custom_prompts: Optional[dict] = None,
) -> dict:
    """Upsert the user's preference row.  None-valued args are ignored."""
    row = db.query(AIUserPreference).filter_by(user_id=user_id).first()
    if row is None:
        row = AIUserPreference(user_id=user_id)
        db.add(row)

    if default_group is not None:
        row.default_group = default_group
    if preferred_vendors is not None:
        row.preferred_vendors = preferred_vendors
    if alert_keywords is not None:
        row.alert_keywords = alert_keywords
    if custom_prompts is not None:
        row.custom_prompts = custom_prompts

    db.commit()
    db.refresh(row)
    return {
        "default_group": row.default_group,
        "preferred_vendors": row.preferred_vendors or [],
        "alert_keywords": row.alert_keywords or [],
        "custom_prompts": row.custom_prompts or {},
    }


# ── Higher-level helper used by the chat loop ─────────────────────────────


# Tool name → memory key mapping.  When a tool succeeds, the chat layer
# calls ``record_tool_memory`` with the tool name and result; this
# function decides which key (if any) to write based on the tool.
_TOOL_TO_MEMORY_KEY = {
    "get_device_config": "last_device_id",
    "list_devices": "last_device_id",
    "search_by_vendor_model": "last_device_id",
    "get_device_neighbors": "last_device_id",
    "get_interface_status": "last_device_id",
    "get_bgp_summary": "last_device_id",
    "get_ospf_neighbors": "last_device_id",
    "get_job": "last_job_id",
    "list_jobs": "last_job_id",
    "get_recent_drifts": "last_drift_id",
    "get_affected_devices": "last_drift_id",
    "get_document": "last_document_id",
}


def record_tool_memory(
    db: Session, *, user_id: int, session_id: str, scope: str,
    tool_name: str, tool_args: dict, tool_result: Any,
) -> Optional[dict]:
    """Inspect a successful tool call and write any obvious memory updates.

    Returns the ``update_memory`` result if a write happened, else None.
    Currently handles device-id, job-id, drift-id and document-id from
    the most common read-only tools.  Write-side tools (create_job,
    acknowledge_drift, etc.) intentionally don't update memory here —
    they need their own confirmation flow.
    """
    key = _TOOL_TO_MEMORY_KEY.get(tool_name)
    if key is None:
        return None

    value = _extract_entity_id(tool_name, tool_args, tool_result)
    if value is None:
        return None

    return update_memory(
        db,
        user_id=user_id,
        session_id=session_id,
        scope=scope,
        key=key,
        value=value,
        memory_type=_memory_type_for_key(key),
        source="tool",
    )


def _memory_type_for_key(key: str) -> str:
    if key.startswith("last_device"):
        return "device_context"
    if key.startswith("last_job"):
        return "task_state"
    if key.startswith("last_drift"):
        return "diagnostic_state"
    if key.startswith("last_document"):
        return "knowledge_state"
    if key.startswith("last_credential"):
        return "credential_state"
    return "device_context"


def _extract_entity_id(
    tool_name: str, tool_args: dict, tool_result: Any,
) -> Optional[dict]:
    """Pull the {device_id, name, ...} blob out of a tool result.

    Tool results are JSON strings (the chat loop stores them as ``content``
    on ``AIChatMessage``), so we tolerate both string and dict inputs.
    """
    payload: Any = tool_result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(payload, dict):
        return None

    if tool_name in {
        "get_device_config", "get_device_neighbors", "get_interface_status",
        "get_bgp_summary", "get_ospf_neighbors",
    }:
        # Tool returns {"device": {"id": ..., "name": ...}} or {"id": ..., "name": ...}
        device = payload.get("device") if "device" in payload else payload
        if not isinstance(device, dict):
            return None
        if "id" not in device:
            return None
        return {
            "device_id": device.get("id"),
            "name": device.get("name") or device.get("hostname") or "",
            "vendor": device.get("vendor"),
        }

    if tool_name in {"list_devices", "search_by_vendor_model"}:
        # These return lists.  If user passed a specific device_id, use that;
        # otherwise use the first hit (or return None — ambiguous).
        if "device_id" in tool_args:
            return {"device_id": tool_args["device_id"], "name": ""}
        items = payload.get("items") or payload.get("devices") or []
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return {
                    "device_id": first.get("id"),
                    "name": first.get("name") or first.get("hostname") or "",
                    "vendor": first.get("vendor"),
                }
        return None

    if tool_name in {"get_job", "list_jobs"}:
        if tool_name == "get_job" and "id" in payload:
            return {"job_id": payload["id"], "kind": payload.get("kind")}
        items = payload.get("items") or payload.get("jobs") or []
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return {"job_id": first.get("id"), "kind": first.get("kind")}

    if tool_name in {"get_recent_drifts", "get_affected_devices"}:
        items = payload.get("items") or payload.get("drifts") or []
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return {
                    "drift_id": first.get("id"),
                    "device_id": first.get("device_id"),
                }
        return None

    if tool_name == "get_document":
        if "id" in payload:
            return {"doc_id": payload["id"], "title": payload.get("title", "")}
        return None

    return None
