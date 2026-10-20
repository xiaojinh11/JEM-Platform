"""AI Monitor service: generates operational analyses of network events.

Architecture
-----------
The monitor lives in three layers:

1. **Trigger** — ``analyze_drift_async(drift_id, db)`` is called from
   ``drift_scheduler.run_once()`` after a new drift is confirmed.  It
   enqueues the work in a background thread (no Celery/Redis needed).

2. **Analysis engine** — ``_run_analysis()`` fetches all context needed for
   a good explanation, then calls the LLM with a tightly-scoped system prompt
   and no tool loop (the analysis is purely text-generation, not a tool caller).

3. **Result storage** — the analysis is written to ``AIAlert`` so the
   frontend can poll ``GET /api/v1/ai/alerts`` or subscribe to
   ``GET /api/v1/ai/alerts/stream`` (SSE) for live updates.

The whole flow is best-effort: if the AI call fails the alert row is
written with status='failed' and the error message, but the drift scan
continues without interruption.

Triggering rules
---------------
- Every ``critical`` drift always triggers analysis.
- ``warning`` drifts trigger analysis only when the AI is enabled.
- Analysis is de-duplicated per (source_kind, source_id) — re-scanning the
  same drift overwrites the old analysis (status='superseded').
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.ai_alert import AIAlert

# ── System prompt for monitor analysis ─────────────────────────────────────────

_ANALYZE_DRIFT_PROMPT = """\
你是一个网络运维 **Monitor（值守分析师）**。有人发现了一台网络设备的配置发生了**未预期变更（配置漂移）**，需要你给出专业的技术分析。

## 分析维度（必须全部覆盖）

1. **变更摘要** — 简洁描述本次变更的内容（加了什么、删了什么、修改了什么配置行）
2. **风险评估** — 这个变更会造成什么影响？
   - 安全性影响（打开了不该有的访问权限吗？泄露了敏感信息吗？）
   - 可用性影响（会导致设备重启、服务中断、或协议震荡吗？）
   - 合规性影响（违反了等保 / 内控配置基线吗？）
   - 分级：🟢低风险 / 🟡中风险 / 🔴高风险（必须给出一个级别）
3. **关联影响** — 同 VLAN / 同网段 / 直连的其它设备是否会受波及？
4. **运维建议** — 应该如何处理？
   - 是否需要立即回滚？
   - 是否需要纳入变更管理流程？
   - 是否需要通知相关人员（安全 / 业务 / 运维）？
5. **配置对比** — 给出变更前后的关键配置行对比（用 diff 格式）。

## 输出格式（严格遵守）

```markdown
## 配置漂移分析报告
设备：{device_name} (ID={device_id})
检测时间：{detected_at}
变更幅度：+{lines_added} 行 -{lines_removed} 行

### 1. 变更摘要
（用 2-3 句话描述变更内容）

### 2. 风险评估
🟢 / 🟡 / 🔴：**{risk_level}**
（详细说明风险点，引用具体配置行）

### 3. 关联影响
（描述同设备历史、同网段邻居、相关 VLAN 等影响范围）

### 4. 运维建议
- [ ] 立即回滚 / 纳入变更 / 通知相关人员（选适用的打勾）

### 5. 配置 diff
```diff
（diff_snippet 内容，无则写"无可用 diff"）
```

---
*本报告由 NAW AI Monitor 自动生成 · {timestamp}*
```

## 上下文（AI 必须参考 user message 里给的 drift 数据作答）

"""

# ── Analysis engine ─────────────────────────────────────────────────────────────


def _build_drift_context(db: "Session", drift_id: int) -> dict:
    """Gather everything the AI needs to explain a drift."""
    from app.models.drift import ConfigDrift
    from app.models.device import Device
    from app.services.configs import list_revisions, read_revision_text

    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        return {"error": f"drift {drift_id} not found"}

    device = db.get(Device, drift.device_id) if drift.device_id else None

    # Recent drifts on the same device (chronological order)
    from sqlalchemy import desc
    recent = (
        db.query(ConfigDrift)
        .filter(ConfigDrift.device_id == drift.device_id, ConfigDrift.id != drift_id)
        .order_by(desc(ConfigDrift.detected_at))
        .limit(5)
        .all()
    )

    # Latest two config revisions for diff
    revisions = list_revisions(db, drift.device_id) if device else []
    old_rev_text = ""
    new_rev_text = ""
    if len(revisions) >= 2:
        old_rev_text = read_revision_text(revisions[1])[:800]
        new_rev_text = read_revision_text(revisions[0])[:800]

    # Device neighbors from Kuzu graph (best-effort)
    graph_neighbors = {}
    try:
        from app.graph import get_graph
        g = get_graph()
        for rel_type, label in [
            ("DRIFT_OF_DEVICE", "Drift"),
            ("DEVICE_USES_CRED", "Credential"),
            ("DEVICE_HAS_CONFIG", "Config"),
        ]:
            res = g.execute(
                f"MATCH (d:Device {{id: $id}})-[:{rel_type}]->(n:{label}) "
                f"RETURN n LIMIT 10",
                {"id": str(drift.device_id)},
            )
            nodes = [row[0] for row in res["rows"] if isinstance(row[0], dict)]
            graph_neighbors[rel_type] = nodes
    except Exception:
        pass

    return {
        "drift": {
            "id": drift.id,
            "severity": drift.severity,
            "change_hint": drift.change_hint,
            "lines_added": drift.lines_added,
            "lines_removed": drift.lines_removed,
            "diff_snippet": (drift.diff_snippet or "")[:600],
            "detected_at": drift.detected_at.isoformat() if drift.detected_at else None,
            "status": drift.status,
        },
        "device": {
            "id": device.id,
            "name": device.name,
            "vendor": device.vendor,
            "model": device.model,
            "role": device.role,
            "group": device.group_name,
        } if device else {},
        "recent_drifts_same_device": [
            {
                "id": r.id,
                "severity": r.severity,
                "change_hint": r.change_hint,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in recent
        ],
        "config_diff": {
            "old_rev_snippet": old_rev_text,
            "new_rev_snippet": new_rev_text,
        },
        "graph_neighbors": graph_neighbors,
    }


def _render_context(ctx: dict) -> str:
    """Turn the context dict into a string to inject into the system prompt."""
    if "error" in ctx:
        return f"（上下文获取失败：{ctx['error']}）"
    parts = []
    d = ctx.get("device", {})
    if d:
        parts.append(f"设备：{d.get('name','?')}  厂商：{d.get('vendor','?')}  型号：{d.get('model','?')}  角色：{d.get('role','?')}")
    dr = ctx.get("drift", {})
    if dr:
        parts.append(
            f"漂移级别：{dr.get('severity','?')}  变更提示：{dr.get('change_hint','?')}  "
            f"+{dr.get('lines_added',0)} -{dr.get('lines_removed',0)} 行"
        )
    diff = ctx.get("config_diff", {})
    if diff.get("old_rev_snippet"):
        parts.append(f"\n--- 旧配置片段 ---\n{diff['old_rev_snippet'][:400]}")
    if diff.get("new_rev_snippet"):
        parts.append(f"\n--- 新配置片段 ---\n{diff['new_rev_snippet'][:400]}")
    diff_snippet = dr.get("diff_snippet", "")
    if diff_snippet:
        parts.append(f"\n--- 漂移 Diff ---\n{diff_snippet[:600]}")
    recent = ctx.get("recent_drifts_same_device", [])
    if recent:
        parts.append(f"\n--- 同设备近期漂移（最近 {len(recent)} 条）---")
        for r in recent:
            parts.append(f"  [{r.get('severity','?')}] {r.get('change_hint','?')} @ {r.get('detected_at','?')}")
    return "\n".join(parts)


def _call_llm_for_analysis(
    system_prompt: str, content: str, cfg: "dict", timeout: int = 120
) -> tuple[str, int, int]:
    """Call the LLM in a single-shot generation (no tool loop).

    Returns (text, tokens_used, duration_ms).
    Raises on network/HTTP error.

    Uses ai_get_cfg_from_session (local to this module) to avoid a circular
    import with app.services.ai.
    """
    from app.core.security import decrypt_secret
    from app.db.session import SessionLocal

    db_for_cfg = SessionLocal()
    try:
        ai_cfg = ai_get_cfg_from_session(db_for_cfg)
    finally:
        db_for_cfg.close()

    if not ai_cfg:
        raise RuntimeError("AI not configured")

    api_key = decrypt_secret(ai_cfg.api_key_enc) if ai_cfg.api_key_enc else None
    if not api_key:
        raise RuntimeError("AI API key not configured")

    base = ai_cfg.base_url.rstrip("/")
    payload = {
        "model": ai_cfg.model,
        "temperature": 0.3,
        "max_tokens": ai_cfg.max_tokens or 2048,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }
    t0 = time.time()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    dur_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM returned HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    msg = data.get("choices", [{}])[0].get("message", {})
    text = msg.get("content") or ""
    return text, tokens, dur_ms


def ai_get_cfg_from_session(db: "Session"):
    """Get AIConfig without triggering full ai.py import."""
    from app.models.ai import AIConfig
    return db.get(AIConfig, 1)


def analyze_drift_async(
    drift_id: int, db: "Session", skip_threshold: bool = False
) -> None:
    """Enqueue a drift analysis in a background thread.

    ``db`` is the session from the caller; it may be closed by the time
    the thread runs, so we open a fresh session inside the thread.
    """
    from app.models.drift import ConfigDrift

    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        logger.warning("analyze_drift_async: drift {} not found", drift_id)
        return

    # De-duplicate: upsert existing alert for this drift.
    from app.db.session import SessionLocal
    alert_db = SessionLocal()
    try:
        existing = (
            alert_db.query(AIAlert)
            .filter_by(source_kind="drift", source_id=drift_id)
            .first()
        )
        if existing:
            existing.status = "superseded"
            existing.updated_at = datetime.now(timezone.utc)
            alert_db.commit()
    finally:
        alert_db.close()

    # Create pending alert row.
    alert_db2 = SessionLocal()
    try:
        alert = AIAlert(
            source_kind="drift",
            source_id=drift_id,
            severity=drift.severity or "warning",
            title=f"配置漂移分析 · {drift.change_hint or 'unknown'} · {drift.device_id}",
            body="",
            status="pending",
            extra={"drift_id": drift_id, "device_id": drift.device_id},
        )
        alert_db2.add(alert)
        alert_db2.commit()
        alert_db2.refresh(alert)
        alert_id = alert.id
    finally:
        alert_db2.close()

    # Run analysis in background thread.
    t = threading.Thread(
        target=_analyze_drift_worker,
        args=(drift_id, alert_id, skip_threshold),
        name=f"ai-monitor-drift-{drift_id}",
        daemon=True,
    )
    t.start()
    logger.debug("enqueued AI drift analysis: drift_id={} alert_id={}", drift_id, alert_id)


def _analyze_drift_worker(drift_id: int, alert_id: int, skip_threshold: bool) -> None:
    """Background thread: does the actual LLM call and writes the result."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        alert = db.get(AIAlert, alert_id)
        if not alert:
            logger.warning("_analyze_drift_worker: alert {} gone", alert_id)
            return

        # Fetch context
        ctx = _build_drift_context(db, drift_id)

        # Build the prompt
        now = datetime.now(timezone.utc)
        system_prompt = _ANALYZE_DRIFT_PROMPT.format(
            device_name=ctx.get("device", {}).get("name", "?"),
            device_id=ctx.get("device", {}).get("id", "?"),
            detected_at=ctx.get("drift", {}).get("detected_at", now.isoformat()),
            lines_added=ctx.get("drift", {}).get("lines_added", 0),
            lines_removed=ctx.get("drift", {}).get("lines_removed", 0),
            risk_level="🟡 中风险",  # placeholder — AI will fill in real level
            timestamp=now.strftime("%Y-%m-%d %H:%M UTC"),
        )
        user_content = (
            "请根据以下上下文，对该配置漂移进行专业分析。\n\n"
            f"=== 上下文 ===\n{_render_context(ctx)}\n"
            "=== 结束 ===\n\n"
            "请严格按照分析报告模板输出，不要省略任何章节。"
        )

        # Call LLM
        try:
            text, tokens, dur_ms = _call_llm_for_analysis(system_prompt, user_content, {})
            alert.body = text
            alert.status = "ready"
            alert.tokens_used = tokens
            alert.duration_ms = dur_ms
            logger.info(
                "AI drift analysis complete: drift_id={} alert_id={} tokens={}ms={}",
                drift_id, alert_id, tokens, dur_ms,
            )
        except Exception as exc:
            alert.status = "failed"
            alert.error = str(exc)[:1000]
            alert.body = ""
            logger.warning("AI drift analysis failed: drift_id={} alert_id={} error={}", drift_id, alert_id, exc)
        finally:
            alert.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
