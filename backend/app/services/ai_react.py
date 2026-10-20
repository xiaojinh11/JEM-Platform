"""ReAct reflection layer for the AI agent tool loop.

The base ``chat()`` function in ``services.ai`` runs a hard-coded
``MAX_AGENT_ROUNDS`` loop with no reflection on partial failure.  This
module adds the diagnostics + fallback strategy that turn the loop
from "give up after 3 rounds" into "think about what's going wrong".

Two capabilities:

1. **Trace** — every (thought -> tool -> observation) step recorded
   on a per-call stack so we can introspect at any point.  Cheap:
   just append to a list per call.  The trace is returned in the
   ``reasoning_trace`` field of the chat response for debugging /
   eval.

2. **Reflection policies** — three strategies, all tunable:

   * ``SameToolRepeatedFailurePolicy``: if the same tool has failed
     twice in a row, this is a deadlock signal — the LLM isn't
     learning from the error message.  Either prompt the LLM with
     a hint to try a different approach, or trigger fallback.

   * ``EmptyResultPolicy``: a tool returned 0 rows / no output.  May
     be legitimate ("no drifts in this group") but if it happened on
     the first try the LLM often doesn't realise — give it a nudge.

   * ``RagFallbackPolicy``: when tools have failed for too long,
     drop the tool requirement and ask the LLM to synthesise from
     the knowledge base via ``rag_search``.  This is the "something
     is better than nothing" safety net the spec calls for.

The reflection policy is pluggable: the chat loop constructs a
:class:`ReActController` and calls ``controller.observe_round(...)``
after each round; the controller returns the next action
(continue / fallback / finish).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal


DecisionKind = Literal["continue", "fallback_rag", "finish", "reflect"]


@dataclass
class ReActStep:
    """One thought -> tool -> observation triple (observation may be error)."""

    round: int
    tool: str
    args: dict
    ok: bool
    observation: str  # first 300 chars of the result, or error message
    duration_ms: int

    def brief(self) -> str:
        status = "OK" if self.ok else "ERR"
        obs = self.observation.replace("\n", " ")[:120]
        return f"[R{self.round} {status} {self.tool} {self.duration_ms}ms] {obs}"


@dataclass
class ReActDecision:
    """What the controller tells the chat loop to do next."""

    kind: DecisionKind
    reason: str = ""
    # When kind == "reflect", a hint to append to the system reminder
    # so the LLM adapts its next round.
    hint: str = ""


@dataclass
class ReActController:
    """In-loop diagnostics for the chat() tool loop.

    Args:
        same_tool_fail_threshold: trigger fallback after N consecutive
            failures of the same tool.
        empty_result_threshold: nudge the LLM after N consecutive
            empty-but-OK tool results.
    """

    same_tool_fail_threshold: int = 2
    empty_result_threshold: int = 2
    enabled: bool = True
    steps: list[ReActStep] = field(default_factory=list)

    def observe_round(self, step: ReActStep) -> ReActDecision:
        """Called after each tool execution. Returns next action."""
        if not self.enabled:
            return ReActDecision("continue", "reflection disabled")

        self.steps.append(step)

        # --- Policy 1: same tool failing in a row ---
        recent = [s for s in self.steps[-self.same_tool_fail_threshold:]]
        if (
            len(recent) >= self.same_tool_fail_threshold
            and all(not s.ok and s.tool == step.tool for s in recent)
        ):
            hint = (
                f"Tool `{step.tool}` has failed {self.same_tool_fail_threshold} times "
                "in a row. Please try a different tool, or use rag_search as a "
                "fallback to retrieve the answer from the manual library."
            )
            return ReActDecision("fallback_rag", hint=hint, reason="same_tool_stuck")

        # --- Policy 2: empty results repeatedly ---
        if len(self.steps) >= self.empty_result_threshold:
            tail = self.steps[-self.empty_result_threshold:]
            if all(s.ok and _looks_empty(s.observation) for s in tail):
                hint = (
                    "Consecutive tools returned empty results. "
                    "Consider tightening the filters (remove vendor/group) "
                    "or use different keywords."
                )
                return ReActDecision("reflect", hint=hint, reason="consecutive_empty")

        return ReActDecision("continue", "within budget")

    def trace_summary(self) -> str:
        """Compact multi-line summary for logging / response."""
        if not self.steps:
            return "(no tool calls)"
        return "\n".join(s.brief() for s in self.steps)


def _looks_empty(observation: str) -> bool:
    """Heuristic: treat a tool output as 'empty' if it claims no results."""
    if not observation:
        return True
    head = observation.strip().lower()[:200]
    empty_signals = (
        '"count": 0', '"count":0',
        "not found", "no matching", "no results",
        '"ok": false',
    )
    return any(sig in head for sig in empty_signals)


# --- RAG fallback helper ---


def run_rag_fallback(
    db: Any,
    user_query: str,
    cfg: Any,
    call_llm: Any,
) -> dict:
    """Single-shot RAG fallback for when the tool loop is stuck.

    Synchronous wrapper around the async ``rag_search`` — this is
    called from the sync ``chat()`` function after the tool loop
    has exhausted rounds.  Always returns ``{"fallback": True,
    "answer": "..."}``; never raises — telemetry / caller already
    saw the failure, surfacing another traceback would just hide
    the user-facing answer.
    """
    import asyncio as _asyncio
    try:
        from app.services.ai_rag import rag_search as _rag

        coro = _rag(
            user_query=user_query,
            cfg=cfg,
            call_llm=call_llm,
            limit=5,
        )
        try:
            # Newer event loops: re-use the running one if any.
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # We are already inside an async context — fallback to a
            # synchronous BM25-only path so we don't deadlock.
            return _sync_rag_fallback(db, user_query, cfg)
        result = _asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        logging.warning("RAG fallback failed: %s", exc)
        return {
            "fallback": True,
            "answer": (
                "Both tool calls and knowledge base search returned no results. "
                "Please try different keywords or check the relevant feature page directly."
            ),
            "ok": False,
        }

    if result.no_recall:
        return {
            "fallback": True,
            "answer": (
                "INFO: AI Agent switched to knowledge base search, but no matching documents found.\n\n"
                + (result.note or "")
            ),
            "ok": True,
            "no_recall": True,
        }

    docs = result.hits[:3]
    chunks: list[str] = []
    for h in docs:
        chunks.append(f"REF **{h.title}** (score={h.score:.2f}, sources={','.join(h.sources)})\n{h.snippet}")
    answer = (
        "INFO: AI Agent tool calls failed consecutively, falling back to knowledge base:\n\n"
        + "\n\n---\n\n".join(chunks)
    )
    return {"fallback": True, "answer": answer, "ok": True, "hits": docs}


def _sync_rag_fallback(db: Any, user_query: str, cfg: Any) -> dict:
    """Synchronous last-resort path — runs only BM25 (no HyDE).

    Used when ``run_rag_fallback`` is invoked from inside a running
    event loop and we cannot await / asyncio.run another coroutine.
    Returns a basic answer shape so the caller doesn't crash.
    """
    try:
        from app.services.ai_rag import search_sparse  # type: ignore
        hits = search_sparse(db, user_query, limit=5)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Sync RAG fallback failed: %s", exc)
        return {
            "fallback": True,
            "answer": "Tool calls and knowledge base search both failed.",
            "ok": False,
        }
    if not hits:
        return {
            "fallback": True,
            "answer": "Tool calls failed and no documents matched in the manual library.",
            "ok": True,
            "no_recall": True,
        }
    chunks = [
        f"REF **{h.get('title', '?')}** (score={h.get('score', 0):.2f})\n{h.get('snippet', '')}"
        for h in hits[:3]
    ]
    return {
        "fallback": True,
        "answer": "INFO: Falling back to knowledge base:\n\n" + "\n\n---\n\n".join(chunks),
        "ok": True,
        "hits": hits[:3],
    }
