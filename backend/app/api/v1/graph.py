"""Graph API endpoints: query and migration.

The visualisation panel talks to the backend through this router. We
expose two endpoints:

* ``GET /graph/stats``            — node/edge counts for the dashboard
* ``GET /graph/subgraph``         — pull a node and its k-hop neighbourhood
* ``POST /graph/query``           — escape hatch: read-only Cypher for power users
* ``POST /graph/rebuild``         — admin-only: re-project SQL -> graph
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, require_admin
from fastapi import Depends
from app.graph import get_graph

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────
# Safety net: strip mutations out of any user-supplied Cypher.
# ─────────────────────────────────────────────────────────────────────

_FORBIDDEN_TOKENS = re.compile(
    r"\b(create|merge|delete|detach|set|remove|drop|copy|load|install|"
    r"export|import|alter|rename|attach)\b",
    re.IGNORECASE,
)


def _assert_readonly(cypher: str) -> None:
    if not cypher.strip():
        raise HTTPException(status_code=400, detail="empty query")
    if _FORBIDDEN_TOKENS.search(cypher):
        raise HTTPException(
            status_code=400,
            detail="only read-only Cypher is allowed (MATCH / RETURN / WITH / WHERE / OPTIONAL MATCH)",
        )


# ─────────────────────────────────────────────────────────────────────
# Stats / overview
# ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def graph_stats(_user=Depends(get_current_user)) -> dict[str, Any]:
    """Counts of nodes and relationship tables."""
    g = get_graph()
    out: dict[str, Any] = {"nodes": {}, "edges": {}}
    for label in ("Device", "Credential", "Document", "User", "Config", "Drift", "ChatMessage"):
        try:
            rows = g.execute(f"MATCH (n:{label}) RETURN count(n)")
            out["nodes"][label] = rows["rows"][0][0] if rows["rows"] else 0
        except Exception:  # noqa: BLE001
            out["nodes"][label] = 0
    for rel in (
        "DEVICE_USES_CRED", "DEVICE_HAS_CONFIG", "CONFIG_OF_DEVICE",
        "DOC_OF_USER", "DRIFT_OF_DEVICE", "DRIFT_BASELINE_OF", "CHAT_OF_USER",
    ):
        try:
            rows = g.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r)")
            out["edges"][rel] = rows["rows"][0][0] if rows["rows"] else 0
        except Exception:  # noqa: BLE001
            out["edges"][rel] = 0
    return out


# ─────────────────────────────────────────────────────────────────────
# Subgraph pull
# ─────────────────────────────────────────────────────────────────────

@router.get("/subgraph")
def subgraph(
    label: str = Query("Device"),
    node_id: str | None = Query(None),
    hops: int = Query(2, ge=1, le=4),
    _user=Depends(get_current_user),
) -> dict[str, Any]:
    """Return a Cytoscape-friendly nodes/edges payload.

    If ``node_id`` is given, walks ``hops`` levels around that node.
    Otherwise returns every node (up to a hard cap) for the initial
    overview render.
    """
    g = get_graph()
    cap = 500  # hard cap to keep payload sane
    nodes: dict[str, dict[str, Any]] = {}
    if node_id:
        # Walk `hops` levels around the target node and include the
        # target itself so the panel has something to draw.
        cypher = (
            f"MATCH (n:{label} {{id: $id}}) "
            f"OPTIONAL MATCH (n)-[*1..{hops}]-(m) "
            f"RETURN n, collect(DISTINCT m) AS neighbours"
        )
        params = {"id": node_id}
        rows = g.execute(cypher, params)
        for n_node, neighbours in rows["rows"]:
            nid = _node_id(n_node) or node_id
            if nid and nid not in nodes:
                nodes[nid] = {"id": nid, "label": label}
            if isinstance(neighbours, list):
                for m in neighbours:
                    mid = _node_id(m)
                    if mid and mid not in nodes:
                        nodes[mid] = {"id": mid, "label": _node_label(m) or ""}
    else:
        cypher = f"MATCH (n:{label}) RETURN n LIMIT {cap}"
        rows = g.execute(cypher)
        for r in rows["rows"]:
            nid = _node_id(r[0])
            if nid and nid not in nodes:
                nodes[nid] = {"id": nid, "label": label}

    # Pull edges between collected nodes
    edges: list[dict[str, Any]] = []
    if len(nodes) > 1:
        ids = list(nodes.keys())
        # Kuzu Cypher supports ``label(r)`` for relationship type
        # but not ``type(r)`` (the latter is Neo4j / OG Cypher).
        edge_cypher = (
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
            "RETURN a.id, label(r), b.id LIMIT 2000"
        )
        erows = g.execute(edge_cypher, {"ids": ids})
        edges = [
            {"source": str(a), "target": str(b), "rel": rel}
            for a, rel, b in erows["rows"]
        ]

    return {"nodes": list(nodes.values()), "edges": edges}


def _node_id(n: Any) -> str | None:
    """Extract the ``id`` property from a Kuzu node payload.

    Kuzu returns nodes as either ``{"id": "...", ...}`` (dict) when
    using ``RETURN n`` directly, or a flat string when returning
    individual properties. We handle both shapes.
    """
    if n is None:
        return None
    if isinstance(n, dict):
        v = n.get("id")
        return str(v) if v is not None else None
    if isinstance(n, str):
        return n
    return None


def _node_label(n: Any) -> str | None:
    if isinstance(n, dict):
        labels = n.get("_label") or n.get("label")
        if isinstance(labels, list) and labels:
            return labels[0]
        if isinstance(labels, str):
            return labels
    return None


# ─────────────────────────────────────────────────────────────────────
# Generic read-only Cypher (admin only)
# ─────────────────────────────────────────────────────────────────────

class QueryBody(BaseModel):
    cypher: str = Field(..., description="Read-only Cypher query")


@router.post("/query")
def run_query(
    body: QueryBody,
    _admin=Depends(require_admin),
) -> dict[str, Any]:
    """Power-user escape hatch: read-only Cypher.

    Restricted to admins because arbitrary graph queries can leak data
    across tenants if we're careless. Strip mutations at the parser.
    """
    _assert_readonly(body.cypher)
    try:
        return get_graph().execute(body.cypher)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────────────
# Rebuild from SQL
# ─────────────────────────────────────────────────────────────────────

@router.post("/rebuild")
def rebuild(_admin=Depends(require_admin)) -> dict[str, Any]:
    """Drop the graph and re-project every row from SQL.

    Used during onboarding (first time the feature is enabled) and as
    a "fix things" button when the graph drifts out of sync.
    """
    from app.graph.sync import bulk_rebuild_from_sql
    n = bulk_rebuild_from_sql()
    return {"nodes_created": n}
