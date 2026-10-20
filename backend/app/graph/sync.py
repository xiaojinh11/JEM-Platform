"""SQLAlchemy -> Kuzu sync hooks.

These are registered as ``event.listens_for(Session, ...)`` handlers so
that every committed write through the ORM is mirrored into the graph
database. The graph is purely derivative — SQL remains the source of
truth — but we want the visualisation panel to reflect live state, so
we keep the sync tight.

Design notes
------------

* We listen at the **session** level (``after_flush``) rather than
  the mapper level. That gives us the freshly-populated state object
  with all column defaults applied, and runs once per flush (so a
  bulk save of N rows costs N small graph ops, not N×M).

* We only mirror entities we care about. Anything else still works
  normally; we just don't surface it in the graph view.

* Failures in the graph layer are logged but do **not** abort the
  SQL transaction. The graph is best-effort: if Kuzu is unreachable
  the API still returns its normal response and the user sees a
  stale graph (acceptable trade-off for not breaking CRUD).

* Edge (relationship) sync is driven by explicit relationships defined
  in ``_EDGE_SPECS``. The key is the source model attribute name; the
  value describes where the edge should go.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Mapper, Session

from app.core.logging import logger
from app.graph import get_graph, reset_graph_cache

# ─────────────────────────────────────────────────────────────────────
# Node projections: model -> (label, props lambda)
# ─────────────────────────────────────────────────────────────────────
#
# Each entry is ``(label, props_fn)`` where ``props_fn`` takes the ORM
# instance and returns a dict of graph properties.
#
# `id` is required — Kuzu needs a primary key on every node.

_NODE_SPECS: dict[str, tuple[str, Any]] = {
    "Device": (
        "Device",
        lambda d: {
            "id": str(d.id),
            "hostname": getattr(d, "hostname", "") or "",
            "vendor": getattr(d, "vendor", "") or "",
            "model": getattr(d, "model", "") or "",
            "ip": getattr(d, "ip", "") or "",
            "status": getattr(d, "status", "") or "",
        },
    ),
    "Credential": (
        "Credential",
        lambda c: {
            "id": str(c.id),
            "name": getattr(c, "name", "") or "",
            "username": getattr(c, "username", "") or "",
            "kind": getattr(c, "kind", "") or "",
        },
    ),
    "Document": (
        "Document",
        lambda doc: {
            "id": str(doc.id),
            "title": getattr(doc, "title", "") or "",
            "kind": getattr(doc, "kind", "") or "",
            "vendor": getattr(doc, "vendor", "") or "",
        },
    ),
    "User": (
        "User",
        lambda u: {
            "id": str(u.id),
            "username": getattr(u, "username", "") or "",
            "role": getattr(u, "role", "") or "",
        },
    ),
}


# ─────────────────────────────────────────────────────────────────────
# Edge projections
# ─────────────────────────────────────────────────────────────────────
#
# Each entry is a 4-tuple: ``(model_attr, edge_rel, from_label, to_label)``.
# After the SQL -> node projection runs, we walk each model_attr on each
# instance and create a ``from_label -[edge_rel]-> to_label`` edge whose
# ``to_id`` comes from the model_attr value. Multiple models can target
# the same edge kind (e.g. ``Device -> Credential`` is mirrored as
# ``DEVICE_USES_CRED``).

_EDGE_SPECS: list[tuple[str, str, str, str]] = [
    # Device.credential_id  -> Credential
    ("Device.credential_id",     "DEVICE_USES_CRED",   "Device",     "Credential"),
    # DriftDeviceLink.device_id -> Device, plus baseline revision join
    ("DriftDeviceLink.device_id","DRIFT_OF_DEVICE",    "Drift",      "Device"),
]


# ─────────────────────────────────────────────────────────────────────
# Hooks
# ─────────────────────────────────────────────────────────────────────

def _model_label(obj: Any) -> str | None:
    """Return the ``_NODE_SPECS`` key for this ORM class, or None."""
    cls = type(obj)
    # Match by class name because the model modules import lazily and
    # identity checks would force us to import them at module load.
    return cls.__name__ if cls.__name__ in _NODE_SPECS else None


def _project_node(obj: Any) -> None:
    label = _model_label(obj)
    if not label:
        return
    node_label, props_fn = _NODE_SPECS[label]
    try:
        props = props_fn(obj)
        get_graph().upsert_node(node_label, props)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph sync failed for {}#{}: {}", label, getattr(obj, "id", "?"), exc)


def _project_delete(obj: Any) -> None:
    label = _model_label(obj)
    if not label:
        return
    try:
        get_graph().delete_node(label, str(obj.id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph delete failed for {}#{}: {}", label, getattr(obj, "id", "?"), exc)


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, flush_context: Any) -> None:
    """Mirror freshly-flushed inserts/updates into the graph."""
    for obj in session.new:
        _project_node(obj)
    for obj in session.dirty:
        # SQLAlchemy fires after_flush before the transaction commits;
        # session.is_modified() is True for any change. We re-upsert
        # which is cheap for MERGE.
        if session.is_modified(obj):
            _project_node(obj)
    # Deletes are processed in after_flush_postexec to ensure FK
    # constraints have settled.


@event.listens_for(Session, "after_flush_postexec")
def _after_flush_postexec(session: Session, flush_context: Any) -> None:
    """Mirror deletions into the graph after the SQL row is gone."""
    for obj in session.deleted:
        _project_delete(obj)


# ─────────────────────────────────────────────────────────────────────
# Migration helpers
# ─────────────────────────────────────────────────────────────────────

def bulk_rebuild_from_sql() -> int:
    """Drop the graph contents and re-project every row from SQL.

    Returns the number of nodes created. Called from a one-shot CLI
    and from the migration endpoint.

    Note: we don't delete the on-disk graph directory because Kuzu
    keeps native handles open on Windows (the dir can't be removed
    while the process holds it). Instead we ``DETACH DELETE`` every
    node — semantically equivalent for the visualisation panel.
    """
    from app.db.session import SessionLocal

    graph = get_graph()
    graph.clear()

    total = 0
    with SessionLocal() as db:
        # Importing here to dodge a circular dependency (graph -> models
        # -> db -> ???). The models import the Base declared in
        # db.session, but only when they're imported themselves.
        from app.models.device import Device
        from app.models.credential import Credential
        from app.models.document import Document
        from app.models.user import User

        # ── node projection ──────────────────────────────────────────
        device_rows = db.query(Device).all()
        for row in device_rows:
            graph.upsert_node("Device", _NODE_SPECS["Device"][1](row))
            total += 1
        for row in db.query(Credential).all():
            graph.upsert_node("Credential", _NODE_SPECS["Credential"][1](row))
            total += 1
        for row in db.query(Document).all():
            graph.upsert_node("Document", _NODE_SPECS["Document"][1](row))
            total += 1
        for row in db.query(User).all():
            graph.upsert_node("User", _NODE_SPECS["User"][1](row))
            total += 1

        # ── edge projection ──────────────────────────────────────────
        # Device -> Credential (credentials.device_id is the FK)
        from app.models.credential import Credential
        for c in db.query(Credential).all():
            if getattr(c, "device_id", None) is None:
                continue
            graph.upsert_edge(
                "DEVICE_USES_CRED", "Device", str(c.device_id),
                "Credential", str(c.id),
            )

        # Document.created_by_user_id -> User
        from app.models.document import Document
        for doc in db.query(Document).all():
            if getattr(doc, "created_by_user_id", None) is None:
                continue
            graph.upsert_edge(
                "DOC_OF_USER", "Document", str(doc.id),
                "User", str(doc.created_by_user_id),
            )

        # Drift -> Device (drift rows link to a device via FK)
        try:
            from app.models.drift import ConfigDrift as DriftModel
            for dr in db.query(DriftModel).all():
                graph.upsert_node("Drift", {
                    "id": str(dr.id),
                    "summary": getattr(dr, "summary", "") or "",
                    "severity": getattr(dr, "severity", "") or "",
                    "device_id": str(getattr(dr, "current_device_id", "")),
                })
                total += 1
                if getattr(dr, "current_device_id", None) is not None:
                    graph.upsert_edge(
                        "DRIFT_OF_DEVICE", "Drift", str(dr.id),
                        "Device", str(dr.current_device_id),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Drift projection skipped: {}", exc)

    logger.info("Graph rebuild: {} nodes", total)
    return total
