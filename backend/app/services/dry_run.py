"""Dry-run preview and rollback plan services.

These are *preview* operations: no SSH, no DB writes beyond reading from
SQL/graph. They feed the AI risk assessment and let the user see what
*would* happen before they hit "approve & start".

``dry_run`` returns the rendered commands per device plus the rule-based
risk verdict; the AI risk comment is added by the caller so we don't
pay an LLM round-trip when the caller just wants to display the result
themselves.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.ai import AIConfig
from app.models.device import Device
from app.models.template import Template
from app.services.ai import _decrypt_api_key, get_config
from app.services.ai_risk import _rule_based_risk
from app.services.templates import TemplateError_, extract_commands, render


def _render_per_device(
    db: Session,
    *,
    commands: Optional[list[str]],
    template_id: Optional[int],
    params: Optional[dict[str, Any]],
    device_ids: list[int],
) -> list[dict[str, Any]]:
    """Resolve a single command list that applies to every device, or render
    a template once and reuse. Per-device templates are out of scope for
    now (the template model is shared across devices)."""
    resolved_template: Optional[Template] = None
    rendered_lines: list[str] = []
    if template_id is not None:
        resolved_template = db.get(Template, template_id)
        if not resolved_template:
            raise ValueError(f"template {template_id} not found")
        try:
            rendered = render(resolved_template.body, params or {})
            rendered_lines = extract_commands(rendered)
        except TemplateError_ as exc:
            raise ValueError(f"template render failed: {exc}") from exc
    elif commands:
        # Accept either a list[str] (the historical dry-run endpoint
        # shape) or a single multi-line string (the inline-commands
        # shape used by ``create_job`` via ``params["__inline_commands"]``
        # — the worker treats it as a multi-line block too, so the risk
        # scan must).
        if isinstance(commands, str):
            rendered_lines = extract_commands(commands)
        else:
            rendered_lines = list(commands)

    if not device_ids:
        return []
    return [
        {"device_id": did, "commands": list(rendered_lines)}
        for did in device_ids
    ]


def dry_run(
    db: Session,
    *,
    job_kind: str,
    commands: Optional[list[str]] = None,
    template_id: Optional[int] = None,
    params: Optional[dict[str, Any]] = None,
    device_ids: list[int],
) -> dict[str, Any]:
    """Compute a dry-run preview.

    Returns:
        dict with keys:
          job_kind, commands_by_device (list of {device_id, commands}),
          risk (rule-based CommandRisk dict),
          estimated_minutes (rough heuristic),
          notes (human-readable advisory strings).
    """
    if job_kind not in {"batch_write", "template_apply", "backup", "config_diff"}:
        # batch_read / collection doesn't write, no risk to preview
        return {
            "job_kind": job_kind,
            "commands_by_device": [],
            "risk": {"is_dangerous": False, "matched_commands": [],
                     "warning": "", "requires_approval": False,
                     "blast_radius": {}, "ai_comment": None},
            "estimated_minutes": 0,
            "notes": ["此任务类型为只读，无需审批"],
        }

    per_device = _render_per_device(
        db,
        commands=commands,
        template_id=template_id,
        params=params,
        device_ids=device_ids,
    )

    # Flatten commands for rule-based scanning. Template_apply renders the
    # same body for every device, so dedup before scanning — otherwise a
    # 100-device job with one "reload" line would show up as 100 matches.
    seen: set[str] = set()
    unique_lines: list[str] = []
    for entry in per_device:
        for cmd in entry["commands"]:
            if cmd in seen:
                continue
            seen.add(cmd)
            unique_lines.append(cmd)

    risk = _rule_based_risk(unique_lines)

    # Rough estimate: 30s per device for SSH round-trip + 1s per command line.
    per_dev_sec = 30 + max(1, len(unique_lines))
    total_minutes = max(1, round((per_dev_sec * len(per_device)) / 60))

    notes: list[str] = []
    if not device_ids:
        notes.append("未选择目标设备，dry-run 仅展示模板渲染结果")
    if risk.is_dangerous:
        notes.append(f"检测到 {len(risk.matched_patterns)} 条高危命令，需要管理员审批")
    if per_device and len(per_device) > 50:
        notes.append(f"目标设备 {len(per_device)} 台，建议拆分批次执行")

    return {
        "job_kind": job_kind,
        "commands_by_device": per_device,
        "risk": {
            "is_dangerous": risk.is_dangerous,
            "matched_commands": risk.matched_patterns,
            "warning": "",
            "requires_approval": risk.require_approval,
            "blast_radius": {},
            "ai_comment": None,
        },
        "estimated_minutes": total_minutes,
        "notes": notes,
    }


# ─────────────────────────────────────────────────────────────────────
# Rollback plan
# ─────────────────────────────────────────────────────────────────────

def build_rollback_plan(
    db: Session,
    *,
    job_id: int,
) -> dict[str, Any]:
    """Compose a human-readable rollback plan from the snapshots that the
    job took before applying changes.

    Strategy ladder:
      1. ``auto_backup_restore`` — every device in the job has a "before"
         snapshot, so we can just push that text back via SSH.
      2. ``config_patch`` — at least one device has a before snapshot but
         not all (e.g. some backups failed). Manual re-collection needed
         for the missing ones.
      3. ``manual`` — no usable snapshot. The user has to rebuild state
         from version control or out-of-band.
    """
    from app.models.job import Job, BackupSnapshot

    job = db.get(Job, job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")

    target_ids = list(job.target_device_ids or [])
    snaps_by_device: dict[int, list[BackupSnapshot]] = {}
    for snap in job.backups or []:
        if snap.trigger == "before":
            snaps_by_device.setdefault(snap.device_id, []).append(snap)

    steps: list[dict[str, Any]] = []
    covered: list[int] = []
    missing: list[int] = []

    for did in target_ids:
        snaps = snaps_by_device.get(did) or []
        if snaps:
            snap = max(snaps, key=lambda s: s.created_at)
            steps.append({
                "device_id": did,
                "action": "restore_backup",
                "snapshot_id": snap.id,
                "storage_path": snap.storage_path,
                "note": snap.note or "",
            })
            covered.append(did)
        else:
            missing.append(did)

    strategy = "manual"
    if covered and not missing:
        strategy = "auto_backup_restore"
    elif covered:
        strategy = "config_patch"

    deps: list[str] = []
    if strategy == "auto_backup_restore":
        deps.append("确认 SSH 凭据可用")
        deps.append("确认备份文件未损坏（compare content_hash）")
    elif strategy == "config_patch":
        deps.append(f"对缺失快照的设备先手动采集当前配置: {missing}")
    else:
        deps.append("无前置快照可用，需通过 git / 配置中心恢复")

    est = 5 + len(covered) * 2 + len(missing) * 15  # minutes

    return {
        "strategy": strategy,
        "steps": steps,
        "missing_devices": missing,
        "estimated_minutes": est,
        "dependencies": deps,
        "generated_at": _iso_now(),
    }


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Post-job AI comparison
# ─────────────────────────────────────────────────────────────────────

def _load_snapshot_text(snap) -> str:
    from pathlib import Path
    try:
        return Path(snap.storage_path).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read snapshot {}: {}", snap.storage_path, exc)
        return ""


def compute_ai_comparison(
    db: Session,
    *,
    job_id: int,
    ai_summary: Optional[str] = None,
) -> dict[str, Any]:
    """Diff every device's before/after snapshots and summarise.

    The verdict ladder:
      - ``failed``        — at least one device has no after snapshot
      - ``major_diff``    — content hash differs AND line diff > 50 lines
      - ``minor_diff``    — content hash differs but small change
      - ``as_expected``   — hashes identical (no device config actually
                             changed; common for idempotent re-runs)
    """
    from app.models.job import Job

    job = db.get(Job, job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")

    deltas: list[dict[str, Any]] = []
    warnings: list[str] = []

    by_dev_before: dict[int, object] = {}
    by_dev_after: dict[int, object] = {}
    for snap in job.backups or []:
        bucket = by_dev_before if snap.trigger == "before" else by_dev_after
        existing = bucket.get(snap.device_id)
        if existing is None or snap.created_at > existing.created_at:
            bucket[snap.device_id] = snap

    failed = False
    for did in job.target_device_ids or []:
        before = by_dev_before.get(did)
        after = by_dev_after.get(did)
        if not after:
            failed = True
            warnings.append(f"device {did}: 缺少 after 快照")
            continue
        if not before:
            warnings.append(f"device {did}: 缺少 before 快照，仅记录 after")
            deltas.append({"device_id": did, "verdict": "no_baseline"})
            continue

        if before.content_hash == after.content_hash:
            deltas.append({
                "device_id": did,
                "verdict": "as_expected",
                "additions": 0,
                "deletions": 0,
                "before_hash": before.content_hash,
                "after_hash": after.content_hash,
            })
            continue

        b_text = _load_snapshot_text(before)
        a_text = _load_snapshot_text(after)
        additions, deletions = _count_diff_lines(b_text, a_text)
        verdict = "major_diff" if (additions + deletions) > 50 else "minor_diff"
        deltas.append({
            "device_id": did,
            "verdict": verdict,
            "additions": additions,
            "deletions": deletions,
            "before_hash": before.content_hash,
            "after_hash": after.content_hash,
        })

    overall = "failed" if failed else (
        "major_diff" if any(d.get("verdict") == "major_diff" for d in deltas)
        else "minor_diff" if any(d.get("verdict") == "minor_diff" for d in deltas)
        else "as_expected"
    )

    return {
        "verdict": overall,
        "deltas": deltas,
        "warnings": warnings,
        "ai_summary": ai_summary,
        "generated_at": _iso_now(),
    }


def _count_diff_lines(left: str, right: str) -> tuple[int, int]:
    """Cheap unified-diff-style count without bringing in another dep.

    Splits on lines and walks both lists; counts additions / deletions
    in the same way the standard difflib would for a small file."""
    import difflib
    sm = difflib.SequenceMatcher(a=left.splitlines(), b=right.splitlines(), autojunk=False)
    adds = dels = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            dels += i2 - i1
        if tag in ("replace", "insert"):
            adds += j2 - j1
    return adds, dels


def summarise_comparison(db: Session, *, job_id: int) -> str:
    """Use the LLM to produce a 2-4 sentence Chinese summary of the
    before/after diff for a finished job. Returns an empty string if the
    AI is disabled or the call fails — callers must treat ``None`` and
    ``""`` equivalently."""
    import httpx

    from app.models.job import Job

    job = db.get(Job, job_id)
    if not job:
        return ""

    # Re-use the comparison builder so we don't recompute line counts
    # with a second diff pass. The LLM only needs the structured facts.
    summary_struct = compute_ai_comparison(db, job_id=job_id, ai_summary=None)
    if summary_struct["verdict"] == "as_expected":
        return "所有设备配置前后无变化，属于幂等执行。"

    cfg: AIConfig = get_config(db)
    api_key = _decrypt_api_key(cfg)
    if not cfg.enabled or not api_key:
        return ""

    payload = {
        "verdict": summary_struct["verdict"],
        "warnings": summary_struct["warnings"][:5],
        "deltas": [
            {"device_id": d.get("device_id"), "verdict": d.get("verdict"),
             "additions": d.get("additions", 0), "deletions": d.get("deletions", 0)}
            for d in summary_struct["deltas"][:20]
        ],
    }
    system = (
        "你是网络运维审计员。给你一段批量任务执行前后的差异摘要，"
        "用 **中文**、不超过 3 句话写一个给审批人看的总结。"
        "重点说明哪些设备的变更属于预期范围、哪些属于异常。"
        "如果 verdict 已经是 as_expected 就只说'无变化'。"
    )
    user = "差异摘要 JSON：\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    req = {
        "model": cfg.model,
        "temperature": 0.2,
        "max_tokens": 240,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=req,
            )
        if resp.status_code >= 400:
            logger.warning("comparison LLM HTTP {}", resp.status_code)
            return ""
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        return content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("comparison LLM call failed: {}", exc)
        return ""
