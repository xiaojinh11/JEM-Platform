"""Tiny runtime-context module for AI tool implementations.

Several AI tools are pure functions of (db, args, username) but they
sometimes need to know the *current* chat session — for example
``propose_config_plan`` stashes its result under a session-scoped key
so concurrent chats don't trample each other. That's the only thing
this module does: expose a :func:`current_session_key` helper that
returns the active ``(user_id, session_id)`` tuple, or ``None`` when
called outside a chat loop.
"""
from __future__ import annotations

from contextvars import ContextVar

#: ``(user_id, session_id)`` of the chat currently running on this
#: request-handling thread/task. Set by ``ai_chat`` before the agent
#: loop starts; cleared in a ``finally`` block.
_current_session: ContextVar[tuple[int, str] | None] = ContextVar(
    "ai_current_session", default=None,
)


def set_session(user_id: int, session_id: str) -> None:
    _current_session.set((user_id, session_id))


def clear_session() -> None:
    _current_session.set(None)


def current_session_key() -> tuple[int, str] | None:
    return _current_session.get()
