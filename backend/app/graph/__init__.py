"""Graph database: store entity relationships for visualization.

This module sits alongside SQLAlchemy and uses Kuzu (an embedded graph DB
that stores everything in a single directory, like SQLite). The design:

- SQLAlchemy remains the source of truth for business data
  (devices, credentials, documents, users).
- Kuzu stores the *relationship graph*: nodes mirror SQL rows,
  edges capture cross-entity references (e.g. which credential belongs
  to which device, which document a device references).
- Sync is one-way: SQL -> graph. SQLAlchemy ``after_insert`` /
  ``after_update`` / ``after_delete`` events fire hooks that project
  the row into the graph.

Why both? Because the relational table is what every existing
endpoint queries (so we don't rewrite the world), but the graph view
gives us a one-query answer to "show me everything connected to
device X" which is what the visualization panel needs.

Graph files live in ``{data_dir}/graph/``. Delete that directory to
rebuild from scratch (the migration script can do that idempotently).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import kuzu

from app.core.config import settings
from app.core.logging import logger

# ─────────────────────────────────────────────────────────────────────
# Module-level singleton + lock
# ─────────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_instance: "GraphClient | None" = None


# ── P0 Security: Cypher label whitelist ───────────────────────────────
# Kuzu doesn't expose parameter binding for DDL/Cypher labels — every
# CREATE TABLE / MATCH / DETACH DELETE builds the label into the query
# string.  Without a whitelist, future code that takes ``label`` from a
# user input becomes an injection point.  This set is the single source
# of truth: anything not in it is rejected before touching the database.
_VALID_NODE_LABELS: frozenset[str] = frozenset({
    "Device", "Credential", "Document", "User",
    "Config", "Drift", "ChatMessage",
})
_VALID_REL_LABELS: frozenset[str] = frozenset({
    "DEVICE_USES_CRED", "DEVICE_HAS_CONFIG", "CONFIG_OF_DEVICE",
    "DOC_OF_USER", "DRIFT_OF_DEVICE", "DRIFT_BASELINE_OF", "CHAT_OF_USER",
})


def _validate_node_label(label: str) -> str:
    if label not in _VALID_NODE_LABELS:
        raise ValueError(f"invalid graph node label: {label!r}")
    return label


def _validate_rel_label(rel: str) -> str:
    if rel not in _VALID_REL_LABELS:
        raise ValueError(f"invalid graph relationship label: {rel!r}")
    return rel


class GraphClient:
    """Thin wrapper around a single Kuzu database + connection.

    Kuzu's ``Database`` and ``Connection`` are not thread-safe to share
    freely, so we serialize access through a lock for write paths.
    Read-only ``execute`` calls go through the same lock — Kuzu itself
    is fine with concurrent reads via separate connections, but a single
    shared connection is simpler and the visualisation panel is the
    only reader anyway.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Kuzu will refuse to open a directory that doesn't exist or is
        # full of unrelated files. Fresh-init lives in ``init_schema``.
        self._db = kuzu.Database(str(self._db_path))
        self._conn = kuzu.Connection(self._db)
        logger.info("Graph database opened at {}", self._db_path)

    # ── schema management ─────────────────────────────────────────────
    def init_schema(self) -> None:
        """Create node / relationship tables idempotently.

        Kuzu ``CREATE`` fails if the table already exists; we therefore
        query ``SHOW_TABLES`` first and skip when present.
        """
        with _lock:
            existing = set(self._exec_flat("CALL show_tables() RETURN name"))
            self._ensure_node(
                existing,
                "Device",
                {"id": "STRING", "hostname": "STRING", "vendor": "STRING",
                 "model": "STRING", "ip": "STRING", "status": "STRING"},
            )
            self._ensure_node(
                existing,
                "Credential",
                {"id": "STRING", "name": "STRING", "username": "STRING",
                 "kind": "STRING"},
            )
            self._ensure_node(
                existing,
                "Document",
                {"id": "STRING", "title": "STRING", "kind": "STRING",
                 "vendor": "STRING"},
            )
            self._ensure_node(
                existing,
                "User",
                {"id": "STRING", "username": "STRING", "role": "STRING"},
            )
            self._ensure_node(
                existing,
                "Config",
                {"id": "STRING", "name": "STRING", "device_id": "STRING",
                 "rev": "INT64"},
            )
            self._ensure_node(
                existing,
                "Drift",
                {"id": "STRING", "summary": "STRING", "severity": "STRING",
                 "device_id": "STRING"},
            )
            self._ensure_node(
                existing,
                "ChatMessage",
                {"id": "STRING", "role": "STRING", "preview": "STRING"},
            )
            self._ensure_rels(existing)

    def _ensure_node(self, existing: set[str], label: str, props: dict[str, str]) -> None:
        label = _validate_node_label(label)
        if label in existing:
            return
        cols = ", ".join(f"{n} {t}" for n, t in props.items())
        self._conn.execute(f"CREATE NODE TABLE {label}({cols}, PRIMARY KEY(id))")
        logger.info("Graph: created node table {}", label)

    def _ensure_rels(self, existing: set[str]) -> None:
        # These mirror the actual ForeignKeys in the SQL schema — every
        # edge here can be backed by a real ``JOIN`` if needed.
        rels = [
            ("DEVICE_USES_CRED",   "Device", "Credential"),
            ("DEVICE_HAS_CONFIG",  "Device", "Config"),
            ("CONFIG_OF_DEVICE",   "Config", "Device"),
            ("DOC_OF_USER",        "Document", "User"),
            ("DRIFT_OF_DEVICE",    "Drift", "Device"),
            ("DRIFT_BASELINE_OF",  "Drift", "Config"),
            ("CHAT_OF_USER",       "ChatMessage", "User"),
        ]
        for name, src, dst in rels:
            if name in existing:
                continue
            # Defense-in-depth: src/dst are hardcoded here, but validate
            # anyway in case a future caller reuses this loop.
            try:
                _validate_rel_label(name)
                _validate_node_label(src)
                _validate_node_label(dst)
            except ValueError as exc:
                logger.error("Graph: skipping invalid relation def {}: {}", name, exc)
                continue
            self._conn.execute(
                f"CREATE REL TABLE {name}(FROM {src} TO {dst})"
            )
            logger.info("Graph: created rel table {}", name)

    # ── helpers ───────────────────────────────────────────────────────
    def _exec_flat(self, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
        res = self._conn.execute(cypher, parameters=params or {})
        out: list[Any] = []
        while res.has_next():
            row = res.get_next()
            out.append(row[0])
        return out

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a Cypher query and return ``{"columns": [...], "rows": [...]}``."""
        with _lock:
            res = self._conn.execute(cypher, parameters=params or {})
            columns = res.get_column_names() if hasattr(res, "get_column_names") else []
            rows: list[list[Any]] = []
            while res.has_next():
                rows.append(res.get_next())
            return {"columns": columns, "rows": rows}

    def clear(self) -> None:
        """Delete every node in every table (DETACH DELETE).

        Used by the rebuild flow. We don't recreate the schema — that's
        the job of ``init_schema`` and the tables are idempotent — but
        we do wipe the data so re-projection starts clean.
        """
        labels = ["Device", "Credential", "Document", "User", "Config"]
        with _lock:
            for label in labels:
                # Reject anything not in the whitelist even though the
                # list above is hardcoded — defense-in-depth against
                # accidental edits turning into a Cypher injection.
                try:
                    _validate_node_label(label)
                except ValueError:
                    continue
                try:
                    self._conn.execute(f"MATCH (n:{label}) DETACH DELETE n")
                except Exception:  # noqa: BLE001
                    # Label may not exist yet; that's fine.
                    pass

    # ── write helpers (called from sync hooks) ────────────────────────
    def upsert_node(self, label: str, props: dict[str, Any]) -> None:
        _validate_node_label(label)
        if "id" not in props:
            raise ValueError(f"upsert_node({label}) requires an 'id' prop")
        # Kuzu's Cypher flavour:
        # - rejects plain ``MERGE ... SET``
        # - rejects ``n += { ... }`` map assignment on ON CREATE
        # - accepts individual ``n.foo = $foo`` on both ON CREATE / ON MATCH
        # So we spell it out field-by-field. The cost is one extra round-trip
        # but the code stays simple and portable across Kuzu versions.
        pairs = ", ".join(f"n.{k} = ${k}" for k in props if k != "id")
        cypher = f"MERGE (n:{label} {{id: $id}})"
        if pairs:
            cypher += f" ON CREATE SET {pairs} ON MATCH SET {pairs}"
        with _lock:
            self._conn.execute(cypher, parameters=props)

    def delete_node(self, label: str, node_id: str) -> None:
        _validate_node_label(label)
        with _lock:
            self._conn.execute(
                f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n",
                parameters={"id": node_id},
            )

    def upsert_edge(self, rel: str, src_label: str, src_id: str,
                    dst_label: str, dst_id: str) -> None:
        _validate_rel_label(rel)
        _validate_node_label(src_label)
        _validate_node_label(dst_label)
        cypher = (
            f"MATCH (s:{src_label} {{id: $src_id}}), (d:{dst_label} {{id: $dst_id}}) "
            f"MERGE (s)-[:{rel}]->(d)"
        )
        with _lock:
            self._conn.execute(
                cypher,
                parameters={"src_id": src_id, "dst_id": dst_id},
            )

    def delete_edge(self, rel: str, src_label: str, src_id: str,
                    dst_label: str, dst_id: str) -> None:
        _validate_rel_label(rel)
        _validate_node_label(src_label)
        _validate_node_label(dst_label)
        cypher = (
            f"MATCH (s:{src_label} {{id: $src_id}})-[r:{rel}]->"
            f"(d:{dst_label} {{id: $dst_id}}) DELETE r"
        )
        with _lock:
            self._conn.execute(
                cypher,
                parameters={"src_id": src_id, "dst_id": dst_id},
            )


# ─────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────

def get_graph() -> GraphClient:
    """Lazy-initialised singleton used across the app.

    Tests / one-off scripts can call ``reset_graph_cache()`` between
    runs to force re-initialisation.
    """
    global _instance
    if _instance is None:
        data_dir = Path(getattr(settings, "data_dir", "./data"))
        graph_dir = data_dir / "graph"
        _instance = GraphClient(graph_dir)
        _instance.init_schema()
    return _instance


def reset_graph_cache() -> None:
    """Drop the cached singleton. Used by the migration script and tests."""
    global _instance
    _instance = None
