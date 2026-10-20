"""AI Agent REST API.

Endpoints (all under /api/v1/ai):
- GET    /config              — fetch settings (api_key is masked)
- PUT    /config              — update settings (admin)
- POST   /config/test         — verify provider connectivity
- GET    /config/stats        — totals for the dashboard cards
- GET    /telemetry           — 7-day cost / latency breakdown (by day/model/user)
- POST   /chat                — one chat turn (sync)
- GET    /history             — fetch past messages for a session
- GET    /sessions            — list sessions for the current user
- DELETE /history             — clear a session
- GET    /agent-logs          — recent tool invocations
- GET    /memory              — read short-term session memory
- DELETE /memory              — clear selected (or all) short-term entries
- GET    /preferences         — read long-term user preferences
- PUT    /preferences         — upsert long-term user preferences
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_admin
from app.models.ai import AIConfig, AIChatMessage, AIAgentLog
from app.models.user import User
from app.services.ai import (
    chat as ai_chat,
    clear_history as ai_clear,
    get_config,
    list_history,
    list_sessions,
    test_connection,
    update_config,
    _decrypt_api_key,
    _parse_models_from_response,
)
from app.core.security import decrypt_secret
import httpx


router = APIRouter(prefix="/ai", tags=["AI"])


# ── Internal helpers ────────────────────────────────────────────────
def _mask_key(k: Optional[str]) -> Optional[str]:
    if not k:
        return None
    if len(k) <= 8:
        return "****"
    return k[:4] + "****" + k[-4:]


def _serialize_config(cfg: AIConfig, *, with_key: bool = False) -> dict:
    return {
        "id": cfg.id,
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "api_key_masked": _mask_key(_decrypt_api_key(cfg)),
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "system_prompt": cfg.system_prompt,
        "enabled": cfg.enabled,
        "active_persona": cfg.active_persona,
        "last_tested_at": cfg.last_tested_at,
        "last_test_ok": cfg.last_test_ok,
        "last_test_message": cfg.last_test_message,
        "total_chats": cfg.total_chats,
        "total_agent_actions": cfg.total_agent_actions,
        "total_tokens": cfg.total_tokens,
        "updated_at": cfg.updated_at,
        "updated_by": cfg.updated_by,
    }


# ── Schemas ─────────────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # sent only on update
    model: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=64, le=32000)
    system_prompt: Optional[str] = None
    enabled: Optional[bool] = None
    active_persona: Optional[str] = None  # chat | agent | monitor


class ChatBody(BaseModel):
    scope: str = Field("chat", pattern="^(chat|agent|monitor)$")
    session_id: str
    message: str
    attachments: Optional[list[dict]] = Field(default=None, description="User-uploaded files/images/URLs/Skills/MCPs for this turn")


class ScriptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="")
    source_code: str = Field(..., min_length=1)
    inputs_schema: Optional[dict] = Field(default=None)
    tags: Optional[str] = Field(default="")
    enabled: bool = Field(default=True)


class ScriptUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    source_code: Optional[str] = None
    inputs_schema: Optional[dict] = None
    tags: Optional[str] = None
    enabled: Optional[bool] = None


# ── Config endpoints ────────────────────────────────────────────────
@router.get("/config")
def get_ai_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cfg = get_config(db)
    return _serialize_config(cfg)


@router.put("/config")
def put_ai_config(
    body: ConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    cfg = update_config(
        db,
        provider=body.provider,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        system_prompt=body.system_prompt,
        enabled=body.enabled,
        active_persona=body.active_persona,
        username=user.username,
    )
    return _serialize_config(cfg)


@router.post("/config/test")
def post_test_ai_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return test_connection(db)


@router.get("/config/models")
def get_ai_models(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manually fetch the model list from the configured provider.
    Returns {"ok": bool, "models": list[str], "message": str}."""
    cfg = get_config(db)
    api_key = _decrypt_api_key(cfg)
    if not api_key:
        return {"ok": False, "models": [], "message": "未配置 API Key"}
    base = cfg.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{base}/v1/models", headers=headers)
        if resp.status_code >= 400:
            return {"ok": False, "models": [], "message": f"上游错误 {resp.status_code}: {resp.text[:200]}"}
        models, _ = _parse_models_from_response(cfg, resp)
        return {"ok": True, "models": models, "message": f"找到 {len(models)} 个模型"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "models": [], "message": f"连接失败: {exc}"}


@router.get("/config/stats")
def get_ai_stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cfg = get_config(db)
    total_logs = db.query(AIAgentLog).count()
    success_logs = db.query(AIAgentLog).filter_by(ok=True).count()
    today_chats = (
        db.query(AIChatMessage)
        .filter(AIChatMessage.role == "user")
        .filter(AIChatMessage.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
        .count()
    )
    return {
        "enabled": cfg.enabled,
        "active_persona": cfg.active_persona,
        "total_chats": cfg.total_chats,
        "total_agent_actions": cfg.total_agent_actions,
        "total_tokens": cfg.total_tokens,
        "today_chats": today_chats,
        "tool_calls_total": total_logs,
        "tool_calls_success": success_logs,
        "tool_success_rate": (success_logs / total_logs) if total_logs else 1.0,
        "last_tested_at": cfg.last_tested_at,
        "last_test_ok": cfg.last_test_ok,
        "last_test_message": cfg.last_test_message,
        "model": cfg.model,
        # Cost dashboard data — read from ai_telemetry_logs.
        # Cheap aggregation; the table is indexed on created_at.
        "telemetry": _stats_telemetry_summary(db, days=7),
    }


def _stats_telemetry_summary(db: Session, *, days: int) -> dict:
    """Compact cost summary inlined into /config/stats for fast render."""
    from app.services.ai_telemetry import summarise
    return summarise(db, days=days)


@router.get("/telemetry")
def get_ai_telemetry(
    days: int = Query(7, ge=1, le=90, description="Aggregation window"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """7/30-day LLM usage breakdown for the cost dashboard.

    Returns ``{today, by_day, by_model, by_user}``.  All counts are
    derived from ``ai_telemetry_logs``; per-row USD cost uses the
    static price table in ``services.ai_telemetry``.  Unknown models
    show ``cost_usd=0`` rather than billing at the wrong rate.
    """
    from app.services.ai_telemetry import summarise
    return summarise(db, days=days)


@router.get("/telemetry/page", response_class=HTMLResponse)
def get_ai_telemetry_page(
    days: int = Query(7, ge=1, le=90),
    user: User = Depends(get_current_user),
):
    """Render a single-page HTML cost dashboard.

    Zero build / zero JS framework dependency: vanilla JS + a tiny
    inline canvas renderer for the bar chart.  Loads data from the
    sibling ``/telemetry`` JSON endpoint.  Suitable for embedding
    in the SPA via iframe or for opening directly during ops.
    """
    return _TELEMETRY_PAGE_HTML


_TELEMETRY_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI Telemetry · Cost Dashboard</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 24px; background: #0f172a; color: #e2e8f0; }
  h1 { font-size: 20px; font-weight: 700; margin: 0 0 16px; }
  .row { display: grid; gap: 16px; margin-bottom: 18px; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px;
          padding: 14px 16px; }
  .label { font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
           color: #94a3b8; margin-bottom: 4px; }
  .value { font-size: 22px; font-weight: 700; color: #f1f5f9; }
  .sub { font-size: 11px; color: #64748b; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; margin-top: 6px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #334155;
           font-size: 13px; }
  th { font-weight: 600; color: #94a3b8; text-transform: uppercase;
       font-size: 11px; letter-spacing: .6px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .grid2 { display: grid; gap: 16px; grid-template-columns: 1fr 1fr; }
  @media (max-width: 720px) { .grid2 { grid-template-columns: 1fr; } }
  canvas { display: block; width: 100%; height: 220px;
           background: #1e293b; border-radius: 6px; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
  .toolbar select { background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
                    border-radius: 4px; padding: 4px 8px; }
  .empty { padding: 20px; color: #64748b; text-align: center; }
  .err { background: #7f1d1d; color: #fee2e2; padding: 10px; border-radius: 6px;
         margin-bottom: 12px; }
</style>
</head>
<body>
<h1>📊 AI Telemetry · Cost Dashboard</h1>

<div class="toolbar">
  <label>Window:
    <select id="days">
      <option value="1">Today</option>
      <option value="7" selected>Last 7 days</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
    </select>
  </label>
  <button id="refresh" style="background:#334155;color:#e2e8f0;border:0;
         border-radius:4px;padding:4px 12px;cursor:pointer;">Refresh</button>
  <span id="status" class="sub"></span>
</div>

<div id="err"></div>

<div id="kpis" class="row kpis"></div>

<div class="grid2">
  <div class="card">
    <div class="label">Daily cost (USD)</div>
    <canvas id="chart"></canvas>
  </div>
  <div class="card">
    <div class="label">By model</div>
    <table id="byModel">
      <thead><tr><th>Model</th><th class="num">Calls</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-top:16px;">
  <div class="label">By user (top 10)</div>
  <table id="byUser">
    <thead><tr><th>User ID</th><th class="num">Calls</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
const $ = (s) => document.querySelector(s);
const fmt = (n, d=2) => Number(n).toLocaleString('en-US', { maximumFractionDigits: d });
const fmtUsd = (n) => '$' + fmt(n, 4);

async function load() {
  const days = Number($('#days').value);
  $('#status').textContent = 'loading...';
  $('#err').innerHTML = '';
  try {
    const r = await fetch(`/api/v1/ai/telemetry?days=${days}`, { credentials: 'same-origin' });
    if (!r.ok) {
      const t = await r.text();
      $('#err').innerHTML = `<div class="err">HTTP ${r.status}: ${t.slice(0, 200)}</div>`;
      $('#status').textContent = '';
      return;
    }
    const data = await r.json();
    render(data);
    $('#status').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    $('#err').innerHTML = `<div class="err">${e.message}</div>`;
    $('#status').textContent = '';
  }
}

function render(data) {
  // KPIs
  const t = data.today || {};
  $('#kpis').innerHTML = `
    <div class="card"><div class="label">Today cost</div><div class="value">${fmtUsd(t.cost_usd || 0)}</div></div>
    <div class="card"><div class="label">Today calls</div><div class="value">${fmt(t.calls || 0, 0)}</div>
      <div class="sub">errors: ${fmt(t.errors || 0, 0)} · fallbacks: ${fmt(t.fallback_calls || 0, 0)}</div></div>
    <div class="card"><div class="label">Today tokens</div><div class="value">${fmt(t.total || 0, 0)}</div>
      <div class="sub">in: ${fmt(t.prompt || 0, 0)} · out: ${fmt(t.completion || 0, 0)}</div></div>
    <div class="card"><div class="label">Window days</div><div class="value">${data.by_day ? data.by_day.length : 0}</div>
      <div class="sub">distinct models: ${data.by_model ? data.by_model.length : 0}</div></div>
  `;

  // Chart
  drawChart(data.by_day || []);

  // Models
  const mb = $('#byModel tbody');
  if (!data.by_model || data.by_model.length === 0) {
    mb.innerHTML = '<tr><td colspan="4" class="empty">No data yet.</td></tr>';
  } else {
    mb.innerHTML = data.by_model.map(m => `
      <tr><td>${m.model}</td>
          <td class="num">${fmt(m.calls, 0)}</td>
          <td class="num">${fmt(m.total, 0)}</td>
          <td class="num">${fmtUsd(m.cost_usd)}</td></tr>
    `).join('');
  }

  // Users (top 10)
  const ub = $('#byUser tbody');
  if (!data.by_user || data.by_user.length === 0) {
    ub.innerHTML = '<tr><td colspan="4" class="empty">No data yet.</td></tr>';
  } else {
    ub.innerHTML = data.by_user.slice(0, 10).map(u => `
      <tr><td>${u.user_id}</td>
          <td class="num">${fmt(u.calls, 0)}</td>
          <td class="num">${fmt(u.total, 0)}</td>
          <td class="num">${fmtUsd(u.cost_usd)}</td></tr>
    `).join('');
  }
}

function drawChart(days) {
  const canvas = $('#chart');
  const ctx = canvas.getContext('2d');
  // Match DPR for crisp rendering
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, W, H);
  if (!days.length) {
    ctx.fillStyle = '#64748b';
    ctx.font = '13px sans-serif';
    ctx.fillText('No data', 10, 20);
    return;
  }

  const padL = 40, padR = 10, padT = 10, padB = 30;
  const w = W - padL - padR, h = H - padT - padB;
  const maxV = Math.max(...days.map(d => d.cost_usd), 0.001);
  const bw = w / days.length;

  // Y-axis grid
  ctx.strokeStyle = '#334155'; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const y = padT + (h * i / 4);
    ctx.moveTo(padL, y); ctx.lineTo(W - padR, y);
  }
  ctx.stroke();

  // Bars
  days.forEach((d, i) => {
    const bh = (d.cost_usd / maxV) * h;
    const x = padL + i * bw + 2;
    const y = padT + h - bh;
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(x, y, bw - 4, bh);
  });

  // X-axis labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  days.forEach((d, i) => {
    const x = padL + i * bw + bw / 2;
    const label = d.date.slice(5); // MM-DD
    ctx.fillText(label, x, H - 10);
  });

  // Y-axis label (max)
  ctx.textAlign = 'right';
  ctx.fillStyle = '#94a3b8';
  ctx.fillText('$' + fmt(maxV, 4), padL - 4, padT + 8);
  ctx.fillText('$0', padL - 4, padT + h);
}

$('#days').addEventListener('change', load);
$('#refresh').addEventListener('click', load);
load();
setInterval(load, 60000); // auto-refresh every 60s
</script>
</body>
</html>
"""


# ── Chat ────────────────────────────────────────────────────────────
@router.post("/chat")
def post_chat(
    body: ChatBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return ai_chat(
        db,
        user_id=user.id,
        username=user.username,
        scope=body.scope,
        session_id=body.session_id,
        user_message=body.message,
        attachments=body.attachments,
    )


# ── History ────────────────────────────────────────────────────────
@router.get("/history")
def get_history(
    scope: str = Query("chat"),
    session_id: str = Query(...),
    limit: int = Query(200, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = list_history(db, user_id=user.id, scope=scope, session_id=session_id, limit=limit)
    return [
        {
            "id": r.id,
            "role": r.role,
            "content": r.content,
            "name": r.name,
            "tool_calls": r.tool_calls,
            "tool_call_id": r.tool_call_id,
            "tokens_used": r.tokens_used,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/sessions")
def get_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return list_sessions(db, user_id=user.id)


@router.delete("/history")
def delete_history(
    scope: str = Query("chat"),
    session_id: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = ai_clear(db, user_id=user.id, scope=scope, session_id=session_id)
    return {"ok": True, "deleted": n}


# ── Agent audit logs ───────────────────────────────────────────────
@router.get("/agent-logs")
def get_agent_logs(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        db.query(AIAgentLog)
        .order_by(AIAgentLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "username": r.username,
            "session_id": r.session_id,
            "tool": r.tool,
            "args": r.args,
            "result_preview": r.result_preview,
            "ok": r.ok,
            "error": r.error,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ── Script CRUD ────────────────────────────────────────────────────
@router.get("/scripts")
def list_scripts(
    keyword: str = Query("", description="按名称或标签搜索"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.ai import AIScript
    from sqlalchemy import or_ as _or
    q = db.query(AIScript)
    if keyword:
        q = q.filter(_or(
            AIScript.name.ilike(f"%{keyword}%"),
            AIScript.description.ilike(f"%{keyword}%"),
            AIScript.tags.ilike(f"%{keyword}%"),
        ))
    rows = q.order_by(AIScript.updated_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "tags": r.tags or "",
            "inputs_schema": r.inputs_schema,
            "version": r.version,
            "enabled": r.enabled,
            "last_run_at": r.last_run_at,
            "last_run_ok": r.last_run_ok,
            "last_run_message": r.last_run_message,
            "total_runs": r.total_runs,
            "total_run_seconds": r.total_run_seconds,
            "created_by": r.created_by,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.post("/scripts", status_code=201)
def create_script(
    body: ScriptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.ai import AIScript
    script = AIScript(
        name=body.name,
        description=body.description,
        source_code=body.source_code,
        inputs_schema=body.inputs_schema,
        tags=body.tags,
        enabled=body.enabled,
        created_by=user.username,
    )
    db.add(script)
    db.commit()
    db.refresh(script)
    return {
        "id": script.id,
        "name": script.name,
        "description": script.description,
        "tags": script.tags or "",
        "enabled": script.enabled,
        "created_by": script.created_by,
        "created_at": script.created_at,
    }


@router.get("/scripts/{script_id}")
def get_script(
    script_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.ai import AIScript
    script = db.get(AIScript, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return {
        "id": script.id,
        "name": script.name,
        "description": script.description,
        "source_code": script.source_code,
        "inputs_schema": script.inputs_schema,
        "tags": script.tags or "",
        "version": script.version,
        "enabled": script.enabled,
        "last_run_at": script.last_run_at,
        "last_run_ok": script.last_run_ok,
        "last_run_message": script.last_run_message,
        "total_runs": script.total_runs,
        "total_run_seconds": script.total_run_seconds,
        "created_by": script.created_by,
        "created_at": script.created_at,
        "updated_at": script.updated_at,
    }


@router.post("/scripts/{script_id}/extract-plan")
def extract_script_plan(
    script_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """AST-extract a ConfigPlan from an uploaded Python automation script.

    Pure static analysis — the script is **never executed** at this
    endpoint. Returns the same ``ConfigPlan`` shape the rest of the
    job pipeline expects, so the frontend can either:

      * preview it directly (Dry-Run against arbitrary device ids), or
      * jump straight to ``POST /jobs`` with ``plan`` set.

    Body (optional):
      ``device_ids``: list[int]  — overrides plan.device_ids for the
                                    preview (the parsed commands don't
                                    have intrinsic targets). Default [].

    Errors:
      * 404 if the script doesn't exist
      * 400 if the parser hard-rejects (malicious import, dangerous
        built-in call, syntax error, > 200KB)
    """
    from app.models.ai import AIScript
    from app.services.plan_builder import (
        assess_plan_risk,
        build_plan_from_script,
    )

    script = db.get(AIScript, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    device_ids: list[int] = []
    if body and isinstance(body, dict):
        raw = body.get("device_ids") or []
        try:
            device_ids = [int(x) for x in raw if str(x).strip()]
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="device_ids 必须是整数列表",
            )

    try:
        plan = build_plan_from_script(
            db, script_id=script_id, device_ids=device_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    assess_plan_risk(db, plan, use_llm=False)

    return plan.to_storage_dict()


@router.put("/scripts/{script_id}")
def update_script(
    script_id: int,
    body: ScriptUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.ai import AIScript
    script = db.get(AIScript, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if body.name is not None: script.name = body.name
    if body.description is not None: script.description = body.description
    if body.source_code is not None:
        script.source_code = body.source_code
        script.version += 1
    if body.inputs_schema is not None: script.inputs_schema = body.inputs_schema
    if body.tags is not None: script.tags = body.tags
    if body.enabled is not None: script.enabled = body.enabled
    db.commit()
    db.refresh(script)
    return {"ok": True, "id": script.id, "version": script.version}


@router.delete("/scripts/{script_id}")
def delete_script(
    script_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    from app.models.ai import AIScript
    script = db.get(AIScript, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    db.delete(script)
    db.commit()
    return {"ok": True}


@router.get("/scripts/{script_id}/runs")
def get_script_runs(
    script_id: int,
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.ai import AIScript, AIScriptRun
    script = db.get(AIScript, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    rows = (
        db.query(AIScriptRun)
        .filter_by(script_id=script_id)
        .order_by(AIScriptRun.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "invoked_by": r.invoked_by,
            "args": r.args,
            "status": r.status,
            "return_code": r.return_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "size_bytes": r.size_bytes,
            "duration_ms": r.duration_ms,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]


# ── AI Alerts (Monitor) ────────────────────────────────────────────
@router.get("/alerts")
def get_alerts(
    limit: int = Query(50, le=200),
    status: str = Query(None, description="Filter: pending | ready | failed | superseded"),
    severity: str = Query(None, description="Filter: critical | warning | info"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List AI-generated alert analyses.

    Returns the most recent alerts from the Monitor (AI analysis of drifts,
    login failures, etc.). Use this for the Monitor persona panel and
    for the notification badge count.
    """
    from app.models.ai_alert import AIAlert
    from sqlalchemy import desc

    q = db.query(AIAlert)
    if status:
        q = q.filter_by(status=status)
    if severity:
        q = q.filter_by(severity=severity)
    rows = q.order_by(desc(AIAlert.created_at)).limit(limit).all()
    return [
        {
            "id": r.id,
            "source_kind": r.source_kind,
            "source_id": r.source_id,
            "severity": r.severity,
            "title": r.title,
            "body": r.body,
            "status": r.status,
            "tokens_used": r.tokens_used,
            "duration_ms": r.duration_ms,
            "error": r.error,
            "extra": r.extra,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/alerts/stream")
async def alerts_sse(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Server-Sent Events stream of new AI alerts.

    Clients open this connection and receive a JSON event each time a new
    alert is written to the ``ai_alerts`` table (drift analysis complete,
    AI error, etc.). The stream terminates when the client disconnects.

    Events:
        event: alert     → {json payload of the new alert}
        event: heartbeat → {"ts": unix_timestamp}  every 25s to keep proxies alive
        event: done      → (no data, stream ended)
    """
    from fastapi.responses import StreamingResponse
    import time as _time

    async def event_gen():
        # Open a fresh session inside the loop — the injected ``db`` is a
        # scoped session bound to the request thread and must not be used
        # inside an async def (SQLAlchemy sessions are synchronous).
        from app.db.session import SessionLocal
        from app.models.ai_alert import AIAlert

        last_seen_id = 0
        poll_interval = 2  # seconds

        while True:
            try:
                loop_db = SessionLocal()
                try:
                    rows = (
                        loop_db.query(AIAlert)
                        .filter(AIAlert.id > last_seen_id)
                        .order_by(AIAlert.id.asc())
                        .limit(20)
                        .all()
                    )
                    for r in rows:
                        last_seen_id = max(last_seen_id, r.id)
                        payload = {
                            "id": r.id,
                            "source_kind": r.source_kind,
                            "source_id": r.source_id,
                            "severity": r.severity,
                            "title": r.title,
                            "body": r.body,
                            "status": r.status,
                            "error": r.error,
                            "extra": r.extra,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                        }
                        import json as _json
                        yield f"event: alert\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"
                finally:
                    loop_db.close()

                import json as _json
                yield f"event: heartbeat\ndata: {_json.dumps({'ts': _time.time()})}\n\n"
                import asyncio as _asyncio
                await _asyncio.sleep(poll_interval)
            except _asyncio.CancelledError:
                import json as _json
                yield f"event: done\ndata: {_json.dumps({'reason': 'client_disconnected'})}\n\n"
                break
            except Exception:
                import asyncio as _asyncio
                await _asyncio.sleep(poll_interval)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/alerts/{alert_id}/reanalyze")
def reanalyze_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-trigger AI analysis for a given alert's source event.

    Useful when the user wants a fresh analysis (e.g. a new config
    revision is available, or the user provided additional context).
    Returns immediately; analysis runs async.
    """
    from app.models.ai_alert import AIAlert
    from app.services.ai_monitor import analyze_drift_async

    alert = db.get(AIAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    if alert.source_kind != "drift":
        return {"ok": False, "message": "Only drift alerts support re-analysis for now"}

    analyze_drift_async(alert.source_id, db, skip_threshold=True)
    alert.status = "pending"
    alert.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "message": "Re-analysis queued"}


@router.get("/alerts/count")
def count_alerts(
    status: str = Query(None),
    severity: str = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Unread/new alert count for the badge."""
    from app.models.ai_alert import AIAlert

    q = db.query(AIAlert)
    if status:
        q = q.filter_by(status=status)
    if severity:
        q = q.filter_by(severity=severity)
    total = q.count()
    unread = q.filter(AIAlert.status == "ready").count()
    return {"total": total, "unread": unread}


# ── Streaming chat ──────────────────────────────────────────────────
@router.post("/chat/stream")
async def stream_chat(
    body: ChatBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Server-Sent Events streaming chat.

    Yields:
        event: error  → {"error": "..."}
        event: reply  → {"content": "..."}  (incremental chunks)
        event: done   → {"tokens": N, "tools_used": [...]}  (final)
    """
    from fastapi.responses import StreamingResponse
    from app.services.ai import chat as ai_chat_sync

    # Run the non-streaming chat in a thread pool so we don't block
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)

    def blocking_chat():
        return ai_chat_sync(
            db,
            user_id=user.id,
            username=user.username,
            scope=body.scope,
            session_id=body.session_id,
            user_message=body.message,
            attachments=body.attachments,
        )

    try:
        result = await loop.run_in_executor(executor, blocking_chat)
    finally:
        executor.shutdown(wait=False)

    if not result.get("ok", False):
        async def error_gen():
            yield f"event: error\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    reply = result.get("reply", "")
    tokens = result.get("tokens", 0)
    tools_used = result.get("tools_used", [])

    async def token_gen():
        # Yield the reply token by token (or chunk by chunk)
        for i in range(0, len(reply), 4):
            chunk = reply[i : i + 4]
            yield f"event: reply\ndata: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)  # yield control
        yield f"event: done\ndata: {json.dumps({'tokens': tokens, 'tools_used': tools_used}, ensure_ascii=False)}\n\n"

    return StreamingResponse(token_gen(), media_type="text/event-stream")


# ── Memory endpoints (short-term session facts + long-term prefs) ──────────


class MemoryPayload(BaseModel):
    facts: dict = Field(default_factory=dict)
    memory_type: str = "device_context"


class MemoryPatchPayload(BaseModel):
    session_id: str
    scope: str = "chat"
    key: str
    value: dict
    memory_type: str = "device_context"
    source: str = "user"
    force: bool = False


class MemoryDeletePayload(BaseModel):
    session_id: str
    scope: str = "chat"
    key: Optional[str] = None  # None = clear all keys for this session/scope


class PreferencesPayload(BaseModel):
    default_group: Optional[str] = None
    preferred_vendors: Optional[list[str]] = None
    alert_keywords: Optional[list[str]] = None
    custom_prompts: Optional[dict[str, str]] = None


@router.get("/memory")
def get_memory(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    session_id: str = Query(...),
    scope: str = Query("chat"),
):
    """Read all short-term memory facts for (user, session, scope).

    Returns a flat ``{key: value}`` map.  Expired rows (>7d) are
    filtered out by the service layer.
    """
    from app.services import ai_memory
    facts = ai_memory.recall_all_memory(
        db,
        user_id=current_user.id,
        session_id=session_id,
        scope=scope,
    )
    return {"facts": facts, "session_id": session_id, "scope": scope}


@router.delete("/memory", status_code=200)
def delete_memory(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    payload: MemoryDeletePayload,
):
    """Clear one key (default) or all keys (key=null) for a session."""
    from app.services import ai_memory
    if payload.key is None:
        # Clear all — caller opted in to a wipe.
        from app.models.ai import AIConversationMemory
        n = (
            db.query(AIConversationMemory)
            .filter_by(
                user_id=current_user.id,
                session_id=payload.session_id,
                scope=payload.scope,
            )
            .delete()
        )
        db.commit()
        return {"cleared": int(n), "scope": "all"}
    deleted = ai_memory.delete_memory(
        db,
        user_id=current_user.id,
        session_id=payload.session_id,
        scope=payload.scope,
        key=payload.key,
    )
    db.commit()
    return {"cleared": int(bool(deleted)), "key": payload.key}


@router.post("/memory/recall", status_code=200)
def recall_single_memory(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    session_id: str = Query(...),
    scope: str = Query("chat"),
    key: str = Query(...),
):
    """Read a single memory fact by key."""
    from app.services import ai_memory
    value = ai_memory.recall_memory(
        db,
        user_id=current_user.id,
        session_id=session_id,
        scope=scope,
        key=key,
    )
    return {"key": key, "value": value, "found": value is not None}


@router.put("/memory", status_code=200)
def put_memory(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    payload: MemoryPatchPayload,
):
    """Upsert a memory fact.  Critical keys with a changed value
    return ``conflict=True`` unless ``force=True``."""
    from app.services import ai_memory
    res = ai_memory.update_memory(
        db,
        user_id=current_user.id,
        session_id=payload.session_id,
        scope=payload.scope,
        key=payload.key,
        value=payload.value,
        memory_type=payload.memory_type,
        source=payload.source,
        force=payload.force,
    )
    db.commit()
    status = 200 if not res.get("conflict") else 409
    # FastAPI will reuse the body for both statuses.
    return res


@router.get("/preferences")
def get_preferences(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read the long-term preference row for the current user."""
    from app.services import ai_memory
    prefs = ai_memory.get_preferences(db, user_id=current_user.id)
    return {"preferences": prefs or {
        "default_group": None,
        "preferred_vendors": [],
        "alert_keywords": [],
        "custom_prompts": {},
    }}


@router.put("/preferences", status_code=200)
def put_preferences(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    payload: PreferencesPayload,
):
    """Upsert the long-term preference row.  Fields with ``null``
    values are left untouched (partial update)."""
    from app.services import ai_memory
    updated = ai_memory.upsert_preferences(
        db,
        user_id=current_user.id,
        default_group=payload.default_group,
        preferred_vendors=payload.preferred_vendors,
        alert_keywords=payload.alert_keywords,
        custom_prompts=payload.custom_prompts,
    )
    return {"preferences": updated}
