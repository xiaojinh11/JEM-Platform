"""Background document parsing + indexing tasks.

The HTTP upload path already:

* Streams the file to a temp file with a hard size cap.
* Computes the SHA-256.
* Copies into the managed storage layout (``{hash[:2]}/{hash}_{id}_fn``).
* Records the row with a placeholder summary.

After the row is committed, the upload endpoint fires ``parse_document``
in the background. The worker reads the file from its managed path with
the same bounded helpers (``smart_read``, ``extract_container``), updates
the ``summary`` column and the Whoosh index, and sets
``index_status='ready'``. If parsing fails, ``index_status='failed'`` is
written so the UI can show a stale entry without crashing.

Splitting parse off the request thread has two benefits:

1. The HTTP response is bounded by streaming + hashing, not by full
   extraction. A 200 MB HDX no longer has to unpack inside the request.
2. Memory pressure is concentrated in the worker process where we can
   observe it (``container_reader`` already caps entries + total chars).
"""
from __future__ import annotations

import traceback

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.document import Document
from app.services.documents import (
    document_storage_path,
    index_document,
    _make_summary,
)
from app.utils.file_reader import smart_read
from app.workers import celery_app


@celery_app.task(name="naw.document.parse", bind=True, max_retries=2)
def parse_document(self, doc_id: int) -> str:
    """Re-read *doc_id* from managed storage and rebuild its index entry.

    Returns the new ``index_status`` value so the caller can log it. The
    function is intentionally idempotent: re-running it on an already
    indexed document just refreshes the entry.
    """
    db: Session = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            logger.warning("parse_document: doc {} no longer exists", doc_id)
            return "missing"

        try:
            path = document_storage_path(doc)
        except ValueError as exc:
            logger.error("parse_document: bad path for doc {}: {}", doc_id, exc)
            doc.index_status = "failed"
            doc.index_error = str(exc)[:500]
            db.commit()
            return "failed"

        body_result = smart_read(path)
        body = body_result.content
        doc.summary = _make_summary(body)

        index_document(
            doc.id,
            doc.title,
            body,
            doc.vendor,
            doc.category,
            doc.summary,
        )
        doc.index_status = "ready"
        doc.index_error = None
        db.commit()
        logger.info(
            "parse_document: doc {} indexed (encoding={}, len={})",
            doc.id,
            body_result.encoding,
            len(body),
        )
        return "ready"
    except Exception as exc:
        # Roll back so the session isn't left in a failed state, then mark
        # the row as failed. We intentionally do not retry automatically;
        # the operator can trigger ``POST /documents/{id}/reindex`` again.
        db.rollback()
        logger.error(
            "parse_document: doc {} failed: {}\n{}",
            doc_id,
            exc,
            traceback.format_exc(),
        )
        try:
            doc = db.get(Document, doc_id)
            if doc is not None:
                doc.index_status = "failed"
                doc.index_error = str(exc)[:500]
                db.commit()
        except Exception:
            db.rollback()
        return "failed"
    finally:
        db.close()
