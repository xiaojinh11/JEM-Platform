"""Tool implementations for the AI Agent.

Each ``_tool_*`` function takes a SQLAlchemy Session and an args dict,
and returns a JSON string.  All exceptions are caught here so the LLM
gets a structured error rather than a traceback.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.core.logging import logger
from app.services.config_parser import parse_config
from app.models.ai import AIScript, AIScriptRun
from app.models.device import Device

HTTP_FETCH_TIMEOUT = 15

# Regexes shared with ai.py for response cleaning
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u0085\u00a0]+", re.UNICODE)
_MULTI_BLANK = re.compile(r"[ \t]{3,}")


    # ── Topology / Graph tools (read-only) ─────────────────────────────

# Whitelist of node labels and relationship types that AI tools are allowed
# to query. Kuzu Cypher interpolates labels and rel types into the query
# string (not parameters), so we MUST validate against this list before
# touching the graph. Anything outside it raises ValueError → returned as
# {"error": "..."} to the LLM, never as a Cypher injection vector.
_ALLOWED_NODE_LABELS: frozenset[str] = frozenset({
    "Device", "Credential", "Document", "User", "Config", "Drift", "ChatMessage",
})
_ALLOWED_REL_TYPES: frozenset[str] = frozenset({
    "DEVICE_USES_CRED", "DEVICE_HAS_CONFIG", "CONFIG_OF_DEVICE",
    "DOC_OF_USER", "DRIFT_OF_DEVICE", "DRIFT_BASELINE_OF", "CHAT_OF_USER",
})

# Map of "what kinds of neighbours the AI can ask for" → rel types that
# connect Device to those labels. Keeps the tool surface tidy and avoids
# exposing every edge in the schema.
_DEVICE_NEIGHBOR_RELS: dict[str, tuple[str, str]] = {
    # neighbour_kind -> (rel_type, neighbour_label)
    "credential":  ("DEVICE_USES_CRED", "Credential"),
    "config":      ("DEVICE_HAS_CONFIG", "Config"),
    "document":    ("DEVICE_HAS_CONFIG", "Config"),  # docs aren't directly linked yet
    "drift":       ("DRIFT_OF_DEVICE",   "Drift"),
}


def _safe_device_id(value: Any) -> int:
    """Validate + coerce a device id from untrusted LLM/tool input.

    Rejects strings with non-digit chars, negative numbers, etc. Returns an
    int on success. Raises ValueError on failure so the dispatcher converts
    it into a JSON error for the LLM.
    """
    if value is None:
        raise ValueError("device_id is required")
    # int-like check without raising on '1' (we want to coerce) but block '1 OR 1=1'
    if isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            raise ValueError(f"device_id must be a positive integer, got {value!r}")
        n = int(s)
    elif isinstance(value, int) and not isinstance(value, bool):
        n = value
    else:
        raise ValueError(f"device_id must be a positive integer, got {value!r}")
    if n <= 0:
        raise ValueError(f"device_id must be positive, got {n}")
    return n


def _tool_get_device_neighbors(db: Session, args: dict) -> str:
    """Walk 1..N hops around a Device and return every connected entity.

    Uses Kuzu Cypher so the actual traversal happens inside the graph DB
    (cheaper than N+1 SQL JOINs). Defaults to 1 hop and all supported
    neighbour kinds.
    """
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    hops = int(args.get("hops") or 1)
    if hops < 1 or hops > 4:
        return json.dumps({"error": "hops must be 1..4"})

    kinds = args.get("kinds") or list(_DEVICE_NEIGHBOR_RELS.keys())
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    kinds = [k for k in kinds if k in _DEVICE_NEIGHBOR_RELS]
    if not kinds:
        return json.dumps({"error": f"kinds must be a non-empty subset of {list(_DEVICE_NEIGHBOR_RELS)}"})

    # 1) Verify the device exists. We use SQL (not graph) because:
    #    - SQL is the source of truth for business state (is_active, etc)
    #    - the graph can lag a few seconds behind the SQL session flush
    from app.models.device import Device
    device = db.get(Device, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})

    # 2) Walk the graph for each requested neighbour kind. Each rel type
    #    is a whitelisted literal so this stays injection-safe.
    neighbours: dict[str, list[dict]] = {k: [] for k in kinds}
    try:
        from app.graph import get_graph
        g = get_graph()
        for kind in kinds:
            rel_type, neighbour_label = _DEVICE_NEIGHBOR_RELS[kind]
            # OPTIONAL MATCH so a device with no neighbours in this kind
            # still returns the device itself.
            cypher = (
                f"MATCH (d:Device {{id: $id}}) "
                f"OPTIONAL MATCH (d)-[:{rel_type}]->(n:{neighbour_label}) "
                f"RETURN n"
            )
            res = g.execute(cypher, {"id": str(device_id)})
            seen: set[str] = set()
            for row in res["rows"]:
                node = row[0]
                if not isinstance(node, dict):
                    continue
                nid = str(node.get("id", ""))
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                item = {"id": nid}
                for k, v in node.items():
                    if k != "id" and v not in (None, ""):
                        item[k] = v
                neighbours[kind].append(item)
    except Exception as exc:
        return json.dumps({"error": f"graph query failed: {exc}"})

    return json.dumps(
        {
            "device": {
                "id": device.id,
                "name": device.name,
                "vendor": device.vendor,
                "model": device.model,
                "role": device.role,
                "group": device.group_name,
            },
            "hops": hops,
            "neighbours": neighbours,
            "totals": {k: len(v) for k, v in neighbours.items()},
        },
        ensure_ascii=False,
    )


def _tool_get_affected_devices(db: Session, args: dict) -> str:
    """Given a drift id, return everything the graph links back to it.

    Currently that's just the device the drift belongs to, but the
    function is structured so we can grow the relation set without
    changing the tool surface.
    """
    try:
        drift_id = _safe_device_id(args.get("drift_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    from app.models.drift import ConfigDrift
    drift = db.get(ConfigDrift, drift_id)
    if not drift:
        return json.dumps({"error": f"drift {drift_id} 不存在"})

    affected: dict[str, list[dict]] = {}
    try:
        from app.graph import get_graph
        g = get_graph()
        # The only rel we currently have is DRIFT_OF_DEVICE — that's the
        # "this drift belongs to that device" edge.
        cypher = (
            "MATCH (dr:Drift {id: $id})-[:DRIFT_OF_DEVICE]->(d:Device) "
            "RETURN d"
        )
        res = g.execute(cypher, {"id": str(drift_id)})
        devs = []
        for row in res["rows"]:
            node = row[0]
            if not isinstance(node, dict):
                continue
            devs.append({
                "id": str(node.get("id", "")),
                "hostname": node.get("hostname", ""),
                "vendor": node.get("vendor", ""),
                "model": node.get("model", ""),
                "status": node.get("status", ""),
            })
        affected["devices"] = devs
        # Pull adjacent drifts on the same device (severity timeline hint)
        same_device_cypher = (
            "MATCH (d:Device {id: $did})<-[:DRIFT_OF_DEVICE]-(other:Drift) "
            "WHERE other.id <> $id "
            "RETURN other ORDER BY other.id DESC LIMIT 20"
        )
        if devs:
            res2 = g.execute(same_device_cypher, {"did": devs[0]["id"], "id": str(drift_id)})
            adj = []
            for row in res2["rows"]:
                node = row[0]
                if not isinstance(node, dict):
                    continue
                adj.append({
                    "id": str(node.get("id", "")),
                    "severity": node.get("severity", ""),
                    "summary": node.get("summary", ""),
                })
            affected["recent_drifts_same_device"] = adj
    except Exception as exc:
        return json.dumps({"error": f"graph query failed: {exc}"})

    return json.dumps(
        {
            "drift": {
                "id": drift.id,
                "severity": drift.severity,
                "change_hint": drift.change_hint,
                "lines_added": drift.lines_added,
                "lines_removed": drift.lines_removed,
                "detected_at": drift.detected_at.isoformat() if drift.detected_at else None,
            },
            "affected": affected,
        },
        ensure_ascii=False,
    )


def _tool_topology_overview(db: Session, args: dict) -> str:
    """Return the network grouped by role, vendor, group_name.

    Counts come from the SQL source of truth (is_active filter matches
    what the rest of the platform shows) so the answer doesn't drift if
    the graph is briefly stale.
    """
    # Import order matters: ``app.models.device`` references ConfigDrift
    # in a relationship, so we MUST register drift first to avoid
    # "expression 'ConfigDrift' failed to locate a name" at mapper init.
    from app.models.drift import ConfigDrift  # noqa: F401  (registers the mapper)
    from app.models.device import Device
    from sqlalchemy import func

    group_by = (args.get("group_by") or "role").strip().lower()
    if group_by not in {"role", "vendor", "group_name", "model"}:
        return json.dumps({"error": "group_by must be one of: role, vendor, group_name, model"})

    col = getattr(Device, group_by)
    rows = (
        db.query(col.label("bucket"), func.count(Device.id).label("count"))
        .filter(Device.is_active.is_(True))
        .group_by(col)
        .all()
    )
    buckets = [
        {"name": (r.bucket or "(none)"), "count": r.count}
        for r in rows
    ]
    buckets.sort(key=lambda x: (-x["count"], x["name"]))

    total = sum(b["count"] for b in buckets)

    drift_rows = (
        db.query(Device.vendor, func.count(ConfigDrift.id))
        .join(ConfigDrift, ConfigDrift.device_id == Device.id)
        .filter(Device.is_active.is_(True))
        .group_by(Device.vendor)
        .all()
    )
    drift_by_vendor = {v or "(none)": c for v, c in drift_rows}

    return json.dumps(
        {
            "group_by": group_by,
            "total_devices": total,
            "buckets": buckets,
            "drift_count_by_vendor": drift_by_vendor,
        },
        ensure_ascii=False,
    )


def _tool_search_by_vendor_model(db: Session, args: dict) -> str:
    """Find devices matching vendor / model filters.

    This is a SQL-backed tool (the graph's Device node has the same
    fields but SQL has is_active and the canonical record). Useful for
    "show me every Cisco 3850 we manage" style queries that feed into
    bulk-config / compliance work.
    """
    from app.models.device import Device
    q = db.query(Device).filter(Device.is_active.is_(True))

    vendor = (args.get("vendor") or "").strip()
    model = (args.get("model") or "").strip()
    role = (args.get("role") or "").strip()
    keyword = (args.get("keyword") or "").strip()
    limit_raw = args.get("limit")
    if limit_raw is None:
        limit = 50
    else:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return json.dumps({"error": "limit must be an integer 1..500"})
    if limit < 1 or limit > 500:
        return json.dumps({"error": "limit must be 1..500"})

    if vendor:
        q = q.filter(Device.vendor.ilike(vendor))
    if model:
        q = q.filter(Device.model.ilike(f"%{model}%"))
    if role:
        q = q.filter(Device.role == role)
    if keyword:
        from sqlalchemy import or_ as _or
        q = q.filter(_or(
            Device.name.ilike(f"%{keyword}%"),
            Device.hostname.ilike(f"%{keyword}%"),
            Device.serial_no.ilike(f"%{keyword}%"),
        ))
    rows = q.order_by(Device.name).limit(limit).all()
    items = [
        {
            "id": r.id,
            "name": r.name,
            "hostname": r.hostname or "",
            "vendor": r.vendor,
            "model": r.model or "",
            "role": r.role,
            "group": r.group_name or "",
            "location": r.location or "",
        }
        for r in rows
    ]
    return json.dumps(
        {"count": len(items), "filters": {"vendor": vendor, "model": model, "role": role, "keyword": keyword}, "devices": items},
        ensure_ascii=False,
    )


# ── Device tools ──────────────────────────────────────────────────────

def _load_latest_config_for_device(db: Session, device_id: int) -> tuple[str, "Device | None"]:
    """Pull the latest config text + the Device row. Returns ("", None) if missing."""
    from app.services.configs import list_revisions, read_revision_text
    device = db.get(Device, device_id)
    if not device:
        return "", None
    revs = list_revisions(db, device_id)
    if not revs:
        return "", device
    return read_revision_text(revs[0]), device


def _tool_get_interface_status(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    ifaces = parsed.get("interfaces") or []

    name_pat = (args.get("name_pattern") or "").strip().lower()
    if name_pat:
        ifaces = [i for i in ifaces if name_pat in (i.get("name") or "").lower()]
    vlan_f = (args.get("vlan_filter") or "").strip()
    if vlan_f:
        ifaces = [i for i in ifaces if (i.get("vlan") or "") == vlan_f]
    if args.get("include_shutdown") is False:
        ifaces = [i for i in ifaces if not i.get("shutdown")]

    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "hostname": parsed.get("hostname") or "",
            "total_interfaces": len(parsed.get("interfaces") or []),
            "filtered_interfaces": len(ifaces),
            "interfaces": ifaces,
            "vlans": parsed.get("vlans") or [],
        },
        ensure_ascii=False,
    )


def _tool_get_bgp_summary(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    bgp = parsed.get("bgp") or {}
    peers = bgp.get("peers") or []
    insecure = [p for p in peers if not p.get("password_set")]
    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "bgp_local_as": bgp.get("local_as") or "",
            "bgp_router_id": bgp.get("router_id") or "",
            "peer_count": len(peers),
            "insecure_peers_without_password": insecure,
            "peers": peers,
        },
        ensure_ascii=False,
    )


def _tool_get_ospf_neighbors(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    processes = parsed.get("ospf") or []
    total_nets = sum(len(p.get("networks") or []) for p in processes)
    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "process_count": len(processes),
            "total_networks": total_nets,
            "processes": processes,
        },
        ensure_ascii=False,
    )

def _load_latest_config_for_device(db: Session, device_id: int) -> tuple[str, "Device | None"]:
    """Pull the latest config text + the Device row. Returns ("", None) if missing."""
    from app.services.configs import list_revisions, read_revision_text
    device = db.get(Device, device_id)
    if not device:
        return "", None
    revs = list_revisions(db, device_id)
    if not revs:
        return "", device
    return read_revision_text(revs[0]), device


def _tool_get_interface_status(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    ifaces = parsed.get("interfaces") or []

    name_pat = (args.get("name_pattern") or "").strip().lower()
    if name_pat:
        ifaces = [i for i in ifaces if name_pat in (i.get("name") or "").lower()]
    vlan_f = (args.get("vlan_filter") or "").strip()
    if vlan_f:
        ifaces = [i for i in ifaces if (i.get("vlan") or "") == vlan_f]
    if args.get("include_shutdown") is False:
        ifaces = [i for i in ifaces if not i.get("shutdown")]

    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "hostname": parsed.get("hostname") or "",
            "total_interfaces": len(parsed.get("interfaces") or []),
            "filtered_interfaces": len(ifaces),
            "interfaces": ifaces,
            "vlans": parsed.get("vlans") or [],
        },
        ensure_ascii=False,
    )


def _tool_get_bgp_summary(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    bgp = parsed.get("bgp") or {}
    peers = bgp.get("peers") or []
    insecure = [p for p in peers if not p.get("password_set")]
    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "bgp_local_as": bgp.get("local_as") or "",
            "bgp_router_id": bgp.get("router_id") or "",
            "peer_count": len(peers),
            "insecure_peers_without_password": insecure,
            "peers": peers,
        },
        ensure_ascii=False,
    )


def _tool_get_ospf_neighbors(db: Session, args: dict) -> str:
    try:
        device_id = _safe_device_id(args.get("device_id"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    text, device = _load_latest_config_for_device(db, device_id)
    if not device:
        return json.dumps({"error": f"设备 {device_id} 不存在"})
    if not text:
        return json.dumps({"error": "该设备无可用配置版本", "device": device.name})

    parsed = parse_config(text, device.vendor or "huawei_vrp")
    processes = parsed.get("ospf") or []
    total_nets = sum(len(p.get("networks") or []) for p in processes)
    return json.dumps(
        {
            "device": {"id": device.id, "name": device.name, "vendor": device.vendor},
            "process_count": len(processes),
            "total_networks": total_nets,
            "processes": processes,
        },
        ensure_ascii=False,
    )

def _tool_list_devices(db: Session, args: dict) -> str:
    q = db.query(Device).filter_by(is_active=True)
    if args.get("vendor"):
        q = q.filter_by(vendor=args["vendor"])
    if args.get("group"):
        q = q.filter_by(group_name=args["group"])
    if args.get("role"):
        q = q.filter_by(role=args["role"])
    kw = args.get("keyword")
    if kw:
        from sqlalchemy import or_ as _or
        q = q.filter(_or(Device.name.ilike(f"%{kw}%"), Device.hostname.ilike(f"%{kw}%")))
    rows = q.limit(int(args.get("limit") or 20)).all()
    items = [
        {
            "id": r.id,
            "name": r.name,
            "vendor": r.vendor,
            "group": r.group_name,
            "model": r.model,
            "role": r.role,
            "location": r.location,
            "ip": next((e.address for e in r.endpoints if e.is_primary), None),
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "devices": items}, ensure_ascii=False)


def _tool_get_device_config(db: Session, args: dict) -> str:
    from app.services.configs import list_revisions, read_revision_text

    device = db.get(Device, int(args["device_id"]))
    if not device:
        return json.dumps({"error": f"设备 {args['device_id']} 不存在"})
    revs = list_revisions(db, device.id)
    if not revs:
        return json.dumps({"error": "无可用配置版本", "device": device.name})
    text = read_revision_text(revs[0])
    max_c = int(args.get("max_chars") or 4000)
    return json.dumps(
        {
            "device": device.name,
            "device_id": device.id,
            "revision_no": revs[0].revision_no,
            "saved_at": revs[0].created_at.isoformat() if revs[0].created_at else None,
            "truncated": len(text) > max_c,
            "config": text[:max_c],
        },
        ensure_ascii=False,
    )


# ── Config / Drift tools ───────────────────────────────────────────────

def _tool_diff_revisions(db: Session, args: dict) -> str:
    from app.services.configs import diff_revisions

    try:
        res = diff_revisions(db, int(args["from_revision_id"]), int(args["to_revision_id"]))
        return json.dumps(
            {"additions": res["additions"], "deletions": res["deletions"], "diff": res["diff_html"][:6000]},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": f"对比失败: {exc}"})


def _tool_recent_drifts(db: Session, args: dict) -> str:
    from app.models.drift import ConfigDrift, DriftStatus
    from sqlalchemy import desc

    q = db.query(ConfigDrift)
    if args.get("severity"):
        q = q.filter_by(severity=args["severity"])
    if args.get("device_id"):
        q = q.filter_by(device_id=int(args["device_id"]))
    q = q.filter_by(status=DriftStatus.NEW.value)
    rows = q.order_by(desc(ConfigDrift.detected_at)).limit(int(args.get("limit") or 10)).all()
    items = [
        {
            "id": r.id,
            "device_id": r.device_id,
            "severity": r.severity,
            "lines_added": r.lines_added,
            "lines_removed": r.lines_removed,
            "change_hint": r.change_hint,
            "diff_snippet": (r.diff_snippet or "")[:800],
            "detected_at": r.detected_at.isoformat(),
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "drifts": items}, ensure_ascii=False)


def _tool_trigger_scan(db: Session) -> str:
    from app.workers.drift_scheduler import run_once

    return json.dumps(run_once(), ensure_ascii=False)


def _tool_ack_drift(db: Session, args: dict, username: str) -> str:
    from app.services.drift import acknowledge_drift

    res = acknowledge_drift(db, int(args["drift_id"]), username)
    if not res:
        return json.dumps({"error": "drift not found"})
    return json.dumps({"ok": True, "drift_id": res.id, "acknowledged_by": res.acknowledged_by})


# ── Credential tools ────────────────────────────────────────────────────

def _tool_list_credentials(db: Session, args: dict) -> str:
    from app.models.credential import Credential

    q = db.query(Credential)
    kw = args.get("keyword")
    if kw:
        q = q.filter(Credential.name.ilike(f"%{kw}%"))
    rows = q.limit(int(args.get("limit") or 20)).all()
    items = [
        {
            "id": r.id,
            "name": r.name,
            "username": r.username,
            "secret_kind": r.secret_kind,
            "description": r.description or "",
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "credentials": items}, ensure_ascii=False)


# ── Job tools ──────────────────────────────────────────────────────────

def _tool_list_jobs(db: Session, args: dict) -> str:
    from app.models.job import Job

    q = db.query(Job)
    if args.get("kind"):
        q = q.filter_by(kind=args["kind"])
    if args.get("status"):
        q = q.filter_by(status=args["status"])
    rows = q.order_by(Job.created_at.desc()).limit(int(args.get("limit") or 10)).all()
    items = [
        {
            "id": r.id,
            "name": r.name,
            "kind": r.kind,
            "status": r.status,
            "initiated_by": r.initiated_by,
            "requires_approval": r.requires_approval,
            "device_count": len(r.target_device_ids or []),
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "jobs": items}, ensure_ascii=False)


def _tool_get_job(db: Session, args: dict) -> str:
    from app.models.job import Job

    job = db.get(Job, int(args["job_id"]))
    if not job:
        return json.dumps({"error": "job not found"})
    steps = [
        {
            "id": s.id,
            "device_id": s.device_id,
            "step_kind": s.step_kind,
            "status": s.status,
            "error": s.error or "",
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in (job.steps or [])
    ]
    return json.dumps(
        {
            "id": job.id,
            "name": job.name,
            "kind": job.kind,
            "status": job.status,
            "initiated_by": job.initiated_by,
            "approved_by": job.approved_by,
            "summary": job.summary or {},
            "steps": steps,
            "created_at": job.created_at.isoformat(),
        },
        ensure_ascii=False,
    )


def _tool_create_job(db: Session, args: dict, username: str) -> str:
    from app.models.job import Job

    kind = args["kind"]
    device_ids: list[int] = [int(x) for x in (args.get("device_ids") or [])]
    if not device_ids:
        return json.dumps({"error": "device_ids 不能为空"}, ensure_ascii=False)
    if kind == "template_apply" and not args.get("template_id"):
        return json.dumps({"error": "template_apply 模式必须提供 template_id"}, ensure_ascii=False)
    job = Job(
        name=args["task_name"],
        kind=kind,
        initiated_by=username,
        target_device_ids=device_ids,
        template_id=int(args["template_id"]) if args.get("template_id") else None,
        status="draft",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return json.dumps(
        {
            "ok": True,
            "job_id": job.id,
            "name": job.name,
            "status": job.status,
            "message": f"任务已创建，ID={job.id}，请在「批量任务」页面审批并启动",
        },
        ensure_ascii=False,
    )


def _tool_cancel_job(db: Session, args: dict, username: str) -> str:
    from app.models.job import Job

    job = db.get(Job, int(args["job_id"]))
    if not job:
        return json.dumps({"error": f"任务 {args['job_id']} 不存在"}, ensure_ascii=False)
    if job.status not in ("queued", "running"):
        return json.dumps({"error": f"任务当前状态为 {job.status}，无法取消"}, ensure_ascii=False)
    job.status = "cancelled"
    db.commit()
    return json.dumps(
        {"ok": True, "job_id": job.id, "status": "cancelled", "message": f"任务 {job.name}（ID={job.id}）已取消"}
    )


# ── Document tools ─────────────────────────────────────────────────────

def _clean_for_tool(text: str, max_chars: int = 400) -> str:
    """Sanitise tool-result snippets before they go back to the LLM."""
    if not text:
        return ""
    text = _CONTROL_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_BLANK.sub("  ", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " …"
    return text


def _tool_search_documents(db: Session, args: dict) -> str:
    """Backward-compat BM25 search (legacy). New code should call rag_search.

    Kept for any tool that explicitly opts out of HyDE; not the default.
    """
    from app.services.documents import search as whoosh_search

    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"count": 0, "documents": [], "note": "查询关键词为空"},
            ensure_ascii=False,
        )

    limit = int(args.get("limit") or 5)
    raw = whoosh_search(query, limit=max(limit * 2, 10))
    hits = [h for h in raw if h.get("score", 0.0) >= 1.0][:limit]

    if not hits:
        return json.dumps(
            {
                "count": 0,
                "documents": [],
                "note": (
                    "知识库中未找到相关内容。请换更具体的关键词重试，"
                    "或去「手册库」页面手动浏览文档列表。"
                ),
            },
            ensure_ascii=False,
        )

    items = [
        {
            "id": h["id"],
            "title": h.get("title", ""),
            "vendor": h.get("vendor") or "",
            "category": h.get("category") or "",
            "snippet": _clean_for_tool(h.get("snippet", ""), max_chars=300),
            "score": round(h.get("score", 0.0), 2),
        }
        for h in hits
    ]
    return json.dumps({"count": len(items), "documents": items}, ensure_ascii=False)


async def _tool_rag_search(db: Session, args: dict) -> str:
    """RAG tool: HyDE + BM25 + (optional) dense + RRF + refusal gate.

    When ``rag_hyde_enabled`` is False we still run multi-recall fusion
    but skip the LLM call, falling back to BM25-only behaviour while
    keeping the API/JSON contract stable.

    Returns JSON consumable by the LLM — see ``RagResult.to_tool_dict``.
    """
    from app.core.config import settings
    from app.models.ai import AIConfig
    from app.services.ai import _call_llm_plain
    from app.services.ai_rag import rag_search as _rag

    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"count": 0, "documents": [], "no_recall": True,
             "note": "查询关键词为空"},
            ensure_ascii=False,
        )

    limit = max(1, min(int(args.get("limit") or 5), 10))

    cfg = db.query(AIConfig).filter_by(scope="agent").order_by(AIConfig.id.desc()).first()
    if cfg is None:
        # No AI config → fall back to plain BM25 path.  We don't want
        # RAG to silently fail when AI is disabled.
        cfg = AIConfig(
            base_url="", api_key_enc="", model="", temperature=0.0, max_tokens=0,
        )

    async def _null_call_llm(*_a, **_k):  # used when HyDE disabled
        return None

    call_llm = _call_llm_plain if settings.rag_hyde_enabled and cfg.api_key_enc else _null_call_llm

    try:
        result = await _rag(
            user_query=query,
            cfg=cfg,
            call_llm=call_llm,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_search failed: %s", exc)
        return json.dumps(
            {"count": 0, "documents": [], "no_recall": True,
             "note": f"检索出错: {exc}"},
            ensure_ascii=False,
        )

    payload = result.to_tool_dict(limit=limit)
    if result.hyde_passage:
        # Show the hypothetical passage to the LLM so it can ground its
        # answer in *that* style.  Truncated to keep JSON small.
        payload["hyde_passage"] = _clean_for_tool(
            result.hyde_passage, max_chars=200,
        )
    return json.dumps(payload, ensure_ascii=False)


def _tool_list_documents(db: Session, args: dict) -> str:
    from app.models.document import Document

    q = db.query(Document)
    if args.get("vendor"):
        q = q.filter(Document.vendor.ilike(f"%{args['vendor']}%"))
    if args.get("category"):
        q = q.filter(Document.category.ilike(f"%{args['category']}%"))
    rows = q.order_by(Document.created_at.desc()).limit(int(args.get("limit") or 50)).all()
    items = [
        {
            "id": r.id,
            "title": _clean_for_tool(r.title or "", max_chars=200),
            "vendor": r.vendor or "",
            "category": _clean_for_tool(r.category or "", max_chars=80),
            "filename": r.filename,
            "version_tag": r.version_tag or "",
            "summary": _clean_for_tool(r.summary or "", max_chars=200),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "documents": items}, ensure_ascii=False)


def _tool_get_document(db: Session, args: dict) -> str:
    from app.models.document import Document

    doc = db.get(Document, int(args["doc_id"]))
    if not doc:
        return json.dumps({"error": f"文档 {args['doc_id']} 不存在"}, ensure_ascii=False)
    return json.dumps(
        {
            "id": doc.id,
            "title": _clean_for_tool(doc.title or "", max_chars=200),
            "vendor": doc.vendor or "",
            "category": _clean_for_tool(doc.category or "", max_chars=80),
            "filename": doc.filename,
            "version_tag": doc.version_tag or "",
            "summary": _clean_for_tool(doc.summary or "", max_chars=800),
            "is_binary": doc.is_binary,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        },
        ensure_ascii=False,
    )


# ── Template tools ─────────────────────────────────────────────────────

def _tool_list_templates(db: Session, args: dict) -> str:
    from app.models.template import Template

    q = db.query(Template)
    kw = args.get("keyword")
    if kw:
        from sqlalchemy import or_ as _or
        q = q.filter(_or(Template.name.ilike(f"%{kw}%"), Template.description.ilike(f"%{kw}%")))
    rows = q.order_by(Template.name).limit(int(args.get("limit") or 20)).all()
    items = [
        {
            "id": r.id,
            "name": _clean_for_tool(r.name or "", max_chars=128),
            "description": _clean_for_tool(r.description or "", max_chars=200),
            "vendor": r.vendor or "",
            "params_schema": r.params_schema or "",
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "templates": items}, ensure_ascii=False)


def _tool_get_template(db: Session, args: dict) -> str:
    from app.models.template import Template

    tpl = db.get(Template, int(args["template_id"]))
    if not tpl:
        return json.dumps({"error": f"模板 {args['template_id']} 不存在"}, ensure_ascii=False)
    return json.dumps(
        {
            "id": tpl.id,
            "name": _clean_for_tool(tpl.name or "", max_chars=128),
            "description": _clean_for_tool(tpl.description or "", max_chars=400),
            "vendor": tpl.vendor or "",
            "params_schema": tpl.params_schema or "",
            "body": _clean_for_tool(tpl.body or "", max_chars=2000),
            "created_by": tpl.created_by,
            "created_at": tpl.created_at.isoformat() if tpl.created_at else None,
        },
        ensure_ascii=False,
    )


# ── Utility tools ──────────────────────────────────────────────────────

def _tool_get_current_time(db: Session) -> str:
    now = datetime.now(timezone.utc)
    return json.dumps({"utc": now.isoformat(), "epoch": int(now.timestamp()), "tz": "UTC"})


def _tool_http_fetch(db: Session, args: dict) -> str:
    url = args.get("url", "")
    max_c = int(args.get("max_chars") or 5000)
    if not url.startswith("http"):
        return json.dumps({"error": "URL must start with http:// or https://"})
    try:
        from app.utils.ssrf import safe_http_get, validate_url, SSRFProtectionError

        try:
            validate_url(url)
        except SSRFProtectionError as exc:
            return json.dumps({"error": f"URL blocked by SSRF protection: {exc}", "url": url})

        result = safe_http_get(url, timeout=HTTP_FETCH_TIMEOUT)
        body = result["body"][:max_c]
        return json.dumps(
            {
                "url": result["url"],
                "status": result["status_code"],
                "content_type": result["content_type"],
                "body": body,
                "truncated": len(result["body"]) > max_c,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": f"fetch failed: {exc}", "url": url})


# ── Script tools ────────────────────────────────────────────────────────

# Session-scoped plan proposals from the LLM. Keyed by (user_id,
# session_id) so concurrent chats don't cross-pollute. The chat loop
# reads this out at the end of its run and attaches the plan to the
# AIChatResponse so the frontend can offer "Dry-Run this AI plan".
_PLAN_PROPOSALS: dict[tuple[int, str], dict] = {}


def _tool_propose_config_plan(db: Session, args: dict, username: str) -> str:
    """LLM-facing: stash a proposed ConfigPlan for the chat response.

    Why a tool instead of parsing markdown?
    --------------------------------------
    The previous design asked the LLM to embed commands in fenced
    code blocks, and we'd regex them out. That was brittle: model
    variance on quoting, comments interleaved, headings inside the
    block, multi-line ``if`` constructs, etc. Each failure mode
    silently dropped commands or, worse, injected commands that
    weren't there.

    With a real tool call the LLM hands us a *typed* payload. We
    validate the structure here, normalise commands, and store the
    plan under the chat session so the chat-loop end-of-run pickup
    can include it in the response.

    Validation:
      * ``commands`` must be a non-empty list of strings
      * each command must be a *literal* line (no obvious runtime
        placeholders — we strip trivial whitespace but reject
        things like ``{vlan_id}`` template syntax that the Worker
        would have to interpret)
      * vendor must be one of the whitelisted adapters (or empty)
    """
    commands = args.get("commands") or []
    if not isinstance(commands, list) or not commands:
        return json.dumps({"error": "commands 必须是字符串数组且不能为空"}, ensure_ascii=False)

    norm_cmds: list[str] = []
    for c in commands:
        if not isinstance(c, str):
            return json.dumps(
                {"error": f"commands 中存在非字符串元素: {type(c).__name__}"},
                ensure_ascii=False,
            )
        line = c.strip()
        if not line:
            continue
        if "{" in line and "}" in line:
            # We refuse templated commands — they need to be rendered
            # via the template engine, not echoed as plan lines.
            return json.dumps(
                {"error": f"命令包含占位符 {{...}}，需要先用模板渲染: {line!r}"},
                ensure_ascii=False,
            )
        norm_cmds.append(line)
    if not norm_cmds:
        return json.dumps({"error": "commands 全部为空行"}, ensure_ascii=False)
    if len(norm_cmds) > 500:
        return json.dumps(
            {"error": f"commands 过长 ({len(norm_cmds)} 行)，单次计划上限 500 行"},
            ensure_ascii=False,
        )

    vendor = (args.get("vendor") or "").strip().lower()
    allowed_vendors = {"", "huawei_vrp", "cisco_ios_xe", "ruijie_rgos", "h3c_comware"}
    if vendor not in allowed_vendors:
        return json.dumps(
            {"error": f"vendor 必须是 {sorted(allowed_vendors - {''})} 之一，收到 {vendor!r}"},
            ensure_ascii=False,
        )

    notes = args.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    notes = [str(n) for n in notes if isinstance(n, (str, int, float))]

    summary = (args.get("summary") or "").strip()
    if not summary:
        summary = f"AI 助手提议 {len(norm_cmds)} 条配置命令"

    plan_dict = {
        "commands": norm_cmds,
        "vendor": vendor,
        "notes": notes,
        "summary": summary,
    }
    # The chat loop reads this at end-of-run. We key on (user, session)
    # so a long-running agent that interleaves multiple sessions can't
    # accidentally pick up another session's proposal.
    from app.services.ai_runtime import current_session_key
    sess_key = current_session_key()
    if sess_key is not None:
        _PLAN_PROPOSALS[sess_key] = plan_dict

    # Reset dry-run lineage hints — we want a fresh plan_id for each
    # AI proposal, even if the LLM retries the tool call mid-session.
    plan_dict["tool_status"] = "ok"
    plan_dict["command_count"] = len(norm_cmds)
    return json.dumps(plan_dict, ensure_ascii=False)


def pop_plan_proposal(sess_key: tuple[int, str] | None) -> dict | None:
    """End-of-chat-run hook: read + clear the latest proposal.

    Returns the proposal dict (or ``None``) and removes it from the
    staging dict so the next chat round starts clean.
    """
    if sess_key is None:
        return None
    return _PLAN_PROPOSALS.pop(sess_key, None)


def _tool_list_scripts(db: Session, args: dict) -> str:
    q = db.query(AIScript).filter_by(enabled=True)
    kw = args.get("keyword")
    if kw:
        from sqlalchemy import or_ as _or
        q = q.filter(_or(AIScript.name.ilike(f"%{kw}%"), AIScript.description.ilike(f"%{kw}%")))
    rows = q.order_by(AIScript.updated_at.desc()).limit(int(args.get("limit") or 20)).all()
    items = [
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "tags": r.tags or "",
            "inputs_schema": r.inputs_schema,
            "version": r.version,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "last_run_ok": r.last_run_ok,
            "last_run_message": r.last_run_message or "",
            "total_runs": r.total_runs,
        }
        for r in rows
    ]
    return json.dumps({"count": len(items), "scripts": items}, ensure_ascii=False)


def _tool_analyze_script(db: Session, args: dict) -> str:
    script = db.get(AIScript, int(args["script_id"]))
    if not script:
        return json.dumps({"error": "script not found"})
    code = script.source_code
    src_lines = code.splitlines()
    imports = [l.strip() for l in src_lines if l.strip().startswith(("import ", "from "))][:15]
    main_count = sum(1 for l in src_lines if re.match(r"^\s*def\s+main\s*\(", l))
    docstring = ""
    for l in src_lines[:10]:
        m = re.search(r'"""(.*?)"""', l)
        if m:
            docstring = m.group(1).strip()
            break

    # ── New: also expose the extracted ConfigPlan preview ──────────
    # AST-only; never executes the script. ``requires_review`` tells
    # the LLM to nudge the user toward a manual confirmation step.
    extraction = {
        "ok": True,
        "commands": [],
        "vendor": "",
        "warnings": [],
        "notes": [],
        "matched_calls": 0,
        "requires_review": False,
    }
    try:
        from app.services.script_parser import parse_script_to_plan
        plan = parse_script_to_plan(code)
        extraction = {
            "ok": True,
            "commands": plan.commands,
            "vendor": plan.vendor,
            "warnings": plan.warnings,
            "notes": plan.notes,
            "matched_calls": plan.matched_calls,
            "requires_review": plan.requires_review,
        }
    except Exception as exc:  # noqa: BLE001  (ScriptParserError etc.)
        extraction = {"ok": False, "error": str(exc)}

    return json.dumps(
        {
            "id": script.id,
            "name": script.name,
            "description": script.description,
            "source_code_preview": code[:800],
            "imports": imports,
            "has_main": main_count > 0,
            "docstring": docstring,
            "lines": len(src_lines),
            "inputs_schema": script.inputs_schema,
            "tags": script.tags or "",
            "last_run_at": script.last_run_at.isoformat() if script.last_run_at else None,
            "last_run_ok": script.last_run_ok,
            "extracted_plan": extraction,
        },
        ensure_ascii=False,
    )


def _tool_run_script(db: Session, args: dict, username: str) -> str:
    script = db.get(AIScript, int(args["script_id"]))
    if not script:
        return json.dumps({"error": "script not found"})
    kwargs = args.get("kwargs") or {}
    max_out = 512 * 1024

    sandbox_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "NAW_SANDBOX": "1",
    }
    sandbox_kwargs = dict(kwargs)
    sandbox_kwargs["__invoked_by__"] = username

    run = AIScriptRun(script_id=script.id, invoked_by=username, args=kwargs, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    t0 = time.time()
    status = "failed"
    return_code = None
    stdout_val = ""
    stderr_val = ""
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "__kwArgs__ = {kwargs}\n"
                "# sandbox guardrail: disable dangerous stdlib\n"
                "import builtins as _b\n"
                "_orig_open = _b.open\n"
                "_WRITABLE_TMP = True\n"
                "def _safe_open(file, mode='r', *a, **kw):\n"
                "    import os as _os\n"
                "    if any(m in mode for m in 'wax+') and not _WRITABLE_TMP:\n"
                "        raise PermissionError('sandbox: writes disabled')\n"
                "    return _orig_open(file, mode, *a, **kw)\n"
                "_b.open = _safe_open\n"
            ).format(kwargs=json.dumps(sandbox_kwargs))
            f.write(script.source_code)
            temp_path = f.name

        result = subprocess.run(
            [sys.executable, "-I", "-S", temp_path],
            capture_output=True,
            timeout=30,
            cwd=tempfile.gettempdir(),
            text=True,
            encoding="utf-8",
            errors="replace",
            env=sandbox_env,
        )
        return_code = result.returncode
        stdout_val = result.stdout[:max_out]
        stderr_val = result.stderr[:max_out]
        status = "success" if return_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        stderr_val = "Script execution timed out (30s limit)"
        status = "timeout"
    except Exception as exc:
        stderr_val = str(exc)[:2000]
        status = "failed"
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    dur_ms = int((time.time() - t0) * 1000)
    run.status = status
    run.return_code = return_code
    run.stdout = stdout_val
    run.stderr = stderr_val
    run.size_bytes = len(stdout_val) + len(stderr_val)
    run.duration_ms = dur_ms
    run.finished_at = datetime.now(timezone.utc)
    db.commit()

    script.total_runs += 1
    script.total_run_seconds += dur_ms / 1000
    script.last_run_at = datetime.now(timezone.utc)
    script.last_run_ok = status == "success"
    script.last_run_message = f"[{status}] {'OK' if status == 'success' else stderr_val[:80]}"
    db.commit()

    return json.dumps(
        {
            "run_id": run.id,
            "script_id": script.id,
            "script_name": script.name,
            "status": status,
            "return_code": return_code,
            "stdout": stdout_val,
            "stderr": stderr_val,
            "duration_ms": dur_ms,
            "size_bytes": run.size_bytes,
        },
        ensure_ascii=False,
    )


# ── Dispatcher ─────────────────────────────────────────────────────────

_TOOL_DISPATCH: dict[str, Any] = {}


def _dispatch_async(fn):
    """Wrap an async tool impl into the sync dispatch shape.

    ``execute_tool`` is synchronous (called from a sync request handler
    path) but a few tools (RAG, future streaming) are async.  We run
    them via ``asyncio.run`` so callers don't have to think about it.
    This is fine because none of these tools do their own concurrency.
    """
    import asyncio

    def wrapper(db, args, username):
        coro = fn(db, args)
        # Each invocation owns its own loop — avoids "loop is closed"
        # when the underlying session/DB is shared across requests.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    return wrapper


def _register_tools() -> None:
    """Populate the dispatch table. Called once at import time."""
    global _TOOL_DISPATCH
    _TOOL_DISPATCH = {
        "list_devices": lambda db, args, _: _tool_list_devices(db, args),
        "get_device_config": lambda db, args, _: _tool_get_device_config(db, args),
        "get_device_neighbors": lambda db, args, _: _tool_get_device_neighbors(db, args),
        "get_affected_devices": lambda db, args, _: _tool_get_affected_devices(db, args),
        "topology_overview": lambda db, args, _: _tool_topology_overview(db, args),
        "search_by_vendor_model": lambda db, args, _: _tool_search_by_vendor_model(db, args),
        "get_interface_status": lambda db, args, _: _tool_get_interface_status(db, args),
        "get_bgp_summary": lambda db, args, _: _tool_get_bgp_summary(db, args),
        "get_ospf_neighbors": lambda db, args, _: _tool_get_ospf_neighbors(db, args),
        "diff_revisions": lambda db, args, _: _tool_diff_revisions(db, args),
        "get_recent_drifts": lambda db, args, _: _tool_recent_drifts(db, args),
        "trigger_drift_scan": lambda db, args, _: _tool_trigger_scan(db),
        "acknowledge_drift": lambda db, args, username: _tool_ack_drift(db, args, username),
        "list_credentials": lambda db, args, _: _tool_list_credentials(db, args),
        "list_jobs": lambda db, args, _: _tool_list_jobs(db, args),
        "get_job": lambda db, args, _: _tool_get_job(db, args),
        "create_job": lambda db, args, username: _tool_create_job(db, args, username),
        "cancel_job": lambda db, args, username: _tool_cancel_job(db, args, username),
        "list_documents": lambda db, args, _: _tool_list_documents(db, args),
        "search_documents": lambda db, args, _: _tool_search_documents(db, args),
        "rag_search": _dispatch_async(_tool_rag_search),
        "get_document": lambda db, args, _: _tool_get_document(db, args),
        "list_templates": lambda db, args, _: _tool_list_templates(db, args),
        "get_template": lambda db, args, _: _tool_get_template(db, args),
        "get_current_time": lambda db, args, _: _tool_get_current_time(db),
        "http_fetch": lambda db, args, _: _tool_http_fetch(db, args),
        "list_scripts": lambda db, args, _: _tool_list_scripts(db, args),
        "analyze_script": lambda db, args, _: _tool_analyze_script(db, args),
        "run_script": lambda db, args, username: _tool_run_script(db, args, username),
        "propose_config_plan": lambda db, args, username: _tool_propose_config_plan(db, args, username),
    }


def execute_tool(db: Session, name: str, args: dict, username: str) -> tuple[str, bool, str | None]:
    """Execute a named tool and return (result_json, ok, error_message)."""
    if not _TOOL_DISPATCH:
        _register_tools()

    if name not in _TOOL_DISPATCH:
        return json.dumps({"error": f"unknown tool: {name}"}), False, f"unknown tool {name}"

    try:
        return _TOOL_DISPATCH[name](db, args, username), True, None
    except Exception as exc:
        logger.warning("AI tool %s failed: %s", name, exc)
        return json.dumps({"error": str(exc)}), False, str(exc)[:1000]
