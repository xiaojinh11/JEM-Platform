"""AI risk assessment service.

Produces two artefacts from a proposed batch of device commands:

* a deterministic *rule-based* risk report (no LLM, never blocks)
* an optional *LLM-assisted* risk comment that explains the flagged
  commands in Chinese, suitable for surfacing in the approval UI.

We deliberately do NOT let the LLM decide whether a job is dangerous
— that's the regex layer's job. The LLM is only used to translate a
machine-readable finding into something a human reviewer can read.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.ai import AIConfig
from app.models.device import Device
from app.services.ai import _decrypt_api_key, get_config
from app.utils.command_validator import (
    CommandRisk,
    assess_commands,
    format_risk_warning,
)

MAX_LLM_RETRIES = 1
RISK_COMMENT_TIMEOUT = 25  # seconds


def _rule_based_risk(commands: list[str]) -> CommandRisk:
    """Always-on deterministic check; runs in <1ms on a few hundred lines."""
    return assess_commands(commands)


def _graph_neighbour_ids(device_id: int) -> list[int]:
    """Return distinct device ids that share a credential, config, or drift
    relation with ``device_id``. Uses Kuzu Cypher; falls back to an empty
    list if the graph is unavailable so a topology outage does not block
    risk assessment."""
    try:
        from app.graph import get_graph
        g = get_graph()
        cypher = (
            "MATCH (d:Device {id: $id}) "
            "OPTIONAL MATCH (d)-[:DEVICE_USES_CRED]->(c:Credential)<-[:DEVICE_USES_CRED]-(n:Device) "
            "WHERE n.id <> d.id "
            "RETURN DISTINCT n.id AS nid"
        )
        res = g.execute(cypher, {"id": str(device_id)})
        ids: list[int] = []
        for row in res.get("rows", []):
            nid = row[0]
            if isinstance(nid, (int, str)) and str(nid).isdigit():
                ids.append(int(nid))
        return ids
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph query failed for {}: {}", device_id, exc)
        return []


def _affected_devices(db: Session, device_ids: list[int]) -> dict[str, Any]:
    """For each target device, list the devices that share its credential —
    anyone whose configuration change might cascade through shared cred /
    OOB access. We only return device ids (lightweight); the UI can pull
    names if needed."""
    targets = [d for d in (db.get(Device, did) for did in device_ids) if d]
    blast_radius: dict[int, dict[str, Any]] = {}
    for d in targets:
        neighbours = _graph_neighbour_ids(d.id)
        blast_radius[d.id] = {
            "name": d.name,
            "vendor": d.vendor,
            "group": d.group_name,
            "neighbours": neighbours,
        }
    return blast_radius


def _strip_code_fence(text: str) -> str:
    """Some models wrap JSON in ```json ... ``` even when asked not to."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].lstrip("`").startswith("json"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _llm_risk_comment(
    db: Session,
    commands: list[str],
    risk: CommandRisk,
    blast_radius: dict[int, dict[str, Any]],
) -> str:
    """Ask the LLM to explain the rule-based findings in 1–3 sentences.

    Returns a plain string.  Raises on transport failure so callers can
    decide to fall back to the rule-only report rather than block the
    whole job.
    """
    cfg: AIConfig = get_config(db)
    api_key = _decrypt_api_key(cfg)
    if not cfg.enabled or not api_key:
        raise RuntimeError("AI 未启用或未配置 API Key")

    payload = {
        "matched_commands": risk.matched_patterns[:10],
        "total_target_devices": len(blast_radius),
        "blast_radius_neighbour_counts": {
            str(did): len(meta.get("neighbours", []))
            for did, meta in blast_radius.items()
        },
    }
    system = (
        "你是网络运维安全顾问。只针对已经由规则引擎标记为高危的命令补充解释。"
        "用 **中文**、不超过 3 句话，给出一条给审批人的快速参考意见。"
        "不要重复规则列表，不要泛泛说'需要注意'。"
        "如果没有高危命令就回复 'NO_RISK'。"
    )
    user = (
        f"以下命令已被正则规则标记为高危：\n```\n"
        + "\n".join(risk.matched_patterns[:10])
        + "\n```\n"
        f"涉及设备 {len(blast_radius)} 台，邻居数："
        f"{json.dumps(payload['blast_radius_neighbour_counts'], ensure_ascii=False)}\n"
        "请基于这些事实给一句简短中文解释。"
    )

    req = {
        "model": cfg.model,
        "temperature": 0.2,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            with httpx.Client(timeout=RISK_COMMENT_TIMEOUT) as client:
                resp = client.post(
                    f"{cfg.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=req,
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status_code}")
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            content = _strip_code_fence(content).strip()
            return content or "NO_RISK"
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_LLM_RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last_exc or RuntimeError("LLM 调用失败")


def assess_job_risk(
    db: Session,
    *,
    commands: list[str],
    device_ids: list[int],
    use_llm: bool = True,
) -> dict[str, Any]:
    """Top-level entry point used by the jobs API.

    Always returns the deterministic verdict. The LLM comment is best-effort:
    if the call fails or AI is disabled, ``ai_comment`` is null and the
    caller continues.
    """
    risk = _rule_based_risk(commands)
    blast_radius = _affected_devices(db, device_ids)

    ai_comment: Optional[str] = None
    if use_llm and risk.is_dangerous:
        try:
            ai_comment = _llm_risk_comment(db, commands, risk, blast_radius)
            if ai_comment and ai_comment.strip().upper() == "NO_RISK":
                ai_comment = None
        except Exception as exc:
            logger.warning("LLM risk comment failed: {}", exc)
            ai_comment = None

    return {
        "is_dangerous": risk.is_dangerous,
        "matched_commands": risk.matched_patterns,
        "warning": format_risk_warning(risk) if risk.is_dangerous else "",
        "requires_approval": risk.require_approval,
        "blast_radius": blast_radius,
        "ai_comment": ai_comment,
    }
