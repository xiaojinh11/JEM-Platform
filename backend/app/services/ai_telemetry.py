"""AI telemetry / cost monitoring.

Records one row per LLM HTTP request so we can compute:
  * token usage (prompt / completion)
  * USD cost (per model)
  * latency
  * fallback path frequency (RAG fallback == tool loop deadlock)

Usage::

    from app.services.ai_telemetry import record_llm_call

    await record_llm_call(
        db,
        session_id=...,
        kind="chat",
        model="gpt-4o-mini",
        prompt_tokens=350,
        completion_tokens=180,
        duration_ms=2400,
        ok=True,
        fallback=False,
    )

Pricing table is approximate (USD/1M tokens) and conservative — refresh
manually when providers change rates.  Unknown models compute cost=0
rather than silently billing at the wrong rate.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.ai import AITelemetryLog


# Approximate USD per 1M tokens (prompt, completion).  Update when
# providers change rates; cost==0 means unknown model.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini":      (0.15, 0.60),
    "gpt-4o":           (2.50, 10.00),
    "gpt-4-turbo":      (10.00, 30.00),
    "gpt-3.5-turbo":    (0.50, 1.50),
    "deepseek-chat":    (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
    "claude-3.5-sonnet": (3.00, 15.00),
    "claude-3-haiku":   (0.25, 1.25),
    "qwen-plus":        (0.80, 2.00),
    "qwen-turbo":       (0.30, 0.60),
    "ollama/":          (0.00, 0.00),  # local -- free
    "local/":           (0.00, 0.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return approximate USD cost for a given model + token counts.

    Strips dated suffixes (e.g. ``gpt-4o-mini-2024-07-18``) by
    prefix-matching against the table.
    """
    if not model:
        return 0.0
    if model in PRICE_TABLE:
        pp, cp = PRICE_TABLE[model]
    else:
        match_key = None
        for k in PRICE_TABLE:
            if model.startswith(k):
                match_key = k
                break
        if match_key is None:
            return 0.0
        pp, cp = PRICE_TABLE[match_key]
    return (prompt_tokens / 1_000_000) * pp + (completion_tokens / 1_000_000) * cp


def record_llm_call(
    db: Any,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
    scope: str = "chat",
    kind: str = "chat",
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
    fallback: bool = False,
) -> None:
    """Persist one telemetry row. Failures are logged, not raised —

    telemetry must never break the chat path.
    """
    try:
        row = AITelemetryLog(
            user_id=user_id,
            session_id=session_id,
            scope=scope,
            kind=kind,
            model=model or "",
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            total_tokens=int(prompt_tokens) + int(completion_tokens),
            duration_ms=int(duration_ms),
            ok=ok,
            error=(error or "")[:1000] or None,
            fallback=fallback,
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logging.warning("telemetry record failed: %s", exc)


# --- Aggregation helpers (used by the dashboard endpoint) ---


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _start_of(day: datetime) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _empty_day_summary() -> dict:
    return {
        "prompt": 0, "completion": 0, "total": 0,
        "cost_usd": 0.0, "calls": 0, "errors": 0, "fallback_calls": 0,
    }


def summarise(db: Any, *, days: int = 7) -> dict[str, Any]:
    """Return aggregate stats for the last ``days`` calendar days.

    Result shape::

        {
          "today": {"prompt": N, "completion": N, "total": N,
                     "cost_usd": F, "calls": N, "errors": N,
                     "fallback_calls": N},
          "by_day": [{"date": "2026-09-25", ...}, ...],   # last `days`
          "by_model": [{"model": "...", "calls": N, "total_tokens": N, "cost_usd": F}, ...],
          "by_user":  [{"user_id": ..., "calls": N, "total_tokens": N, "cost_usd": F}, ...],
        }
    """
    cutoff = _utcnow() - timedelta(days=days - 1)
    cutoff = _start_of(cutoff)

    rows: list[AITelemetryLog] = (
        db.query(AITelemetryLog)
        .filter(AITelemetryLog.created_at >= cutoff)
        .all()
    )
    if not rows:
        return {
            "today": _empty_day_summary(),
            "by_day": [],
            "by_model": [],
            "by_user": [],
        }

    today_start = _start_of(_utcnow())

    # --- Per-day buckets ---
    by_day: dict[str, dict] = defaultdict(_empty_day_summary)
    for r in rows:
        day = _start_of(r.created_at).date().isoformat()
        agg = by_day[day]
        agg["prompt"] += r.prompt_tokens
        agg["completion"] += r.completion_tokens
        agg["total"] += r.total_tokens
        agg["cost_usd"] += estimate_cost_usd(r.model, r.prompt_tokens, r.completion_tokens)
        agg["calls"] += 1
        if not r.ok:
            agg["errors"] += 1
        if r.fallback:
            agg["fallback_calls"] += 1

    by_day_list = [
        {"date": d, **summary} for d, summary in sorted(by_day.items())
    ]

    # --- Per-model buckets ---
    by_model: dict[str, dict] = defaultdict(_empty_day_summary)
    for r in rows:
        agg = by_model[r.model or "(unknown)"]
        agg["total"] += r.total_tokens
        agg["prompt"] += r.prompt_tokens
        agg["completion"] += r.completion_tokens
        agg["cost_usd"] += estimate_cost_usd(r.model, r.prompt_tokens, r.completion_tokens)
        agg["calls"] += 1
    by_model_list = sorted(
        ({"model": k, **v} for k, v in by_model.items()),
        key=lambda x: x["calls"], reverse=True,
    )

    # --- Per-user buckets ---
    by_user: dict[int, dict] = defaultdict(_empty_day_summary)
    for r in rows:
        uid = r.user_id or 0
        agg = by_user[uid]
        agg["total"] += r.total_tokens
        agg["cost_usd"] += estimate_cost_usd(r.model, r.prompt_tokens, r.completion_tokens)
        agg["calls"] += 1
    by_user_list = sorted(
        ({"user_id": k, **v} for k, v in by_user.items()),
        key=lambda x: x["cost_usd"], reverse=True,
    )

    today_str = today_start.date().isoformat()
    today_agg = by_day.get(today_str) or _empty_day_summary()

    return {
        "today": today_agg,
        "by_day": by_day_list,
        "by_model": by_model_list,
        "by_user": by_user_list,
    }
