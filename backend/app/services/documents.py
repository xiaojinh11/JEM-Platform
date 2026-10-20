"""Document import + Whoosh-based search + Obsidian mirror."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown as md_lib
from sqlalchemy.orm import Session
from whoosh import index
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import ID, TEXT, Schema
from whoosh.qparser import MultifieldParser, QueryParser

from app.core.config import settings
from app.core.logging import logger
from app.models.document import Document
from app.services import obsidian as obsidian_service
from app.utils.file_reader import smart_read
from app.utils.helpers import ensure_dir, managed_path, safe_filename

SCHEMA = Schema(
    id=ID(stored=True, unique=True),
    title=TEXT(stored=True),
    body=TEXT(analyzer=StemmingAnalyzer()),
    vendor=TEXT(stored=True),
    category=TEXT(stored=True),
    summary=TEXT(stored=True),
)

_INDEX_DIR: Optional[Path] = None


def _index_dir() -> Path:
    global _INDEX_DIR
    if _INDEX_DIR is None:
        _INDEX_DIR = ensure_dir(settings.storage_root / "index")
    return _INDEX_DIR


def _get_writer():
    ix = _ensure_index()
    return ix.writer()


def _ensure_index():
    d = _index_dir()
    if not index.exists_in(str(d)):
        return index.create_in(str(d), SCHEMA)
    return index.open_dir(str(d))


def index_document(doc_id: int, title: str, body: str, vendor: Optional[str], category: Optional[str], summary: Optional[str]) -> None:
    try:
        writer = _get_writer()
        writer.update_document(id=str(doc_id), title=title, body=body, vendor=vendor or "", category=category or "", summary=summary or "")
        writer.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("index_document failed: {}", exc)


def remove_document(doc_id: int) -> None:
    try:
        writer = _get_writer()
        writer.delete_by_term("id", str(doc_id))
        writer.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("remove_document failed: {}", exc)


def search(query: str, limit: int = 20) -> list[dict]:
    """BM25 search across title / body / summary.

    Returns a list of dicts with id / title / vendor / category /
    snippet / score. ``snippet`` is a 240-char window around the first
    matched term in the document body (or the stored summary if the
    body isn't stored in the schema).
    """
    if not query.strip():
        return []
    ix = _ensure_index()
    out: list[dict] = []
    with ix.searcher() as searcher:
        parser = MultifieldParser(["title", "body", "summary"], schema=ix.schema)
        try:
            q = parser.parse(query)
        except Exception:  # noqa: BLE001
            q = QueryParser("body", schema=ix.schema).parse(query)
        # ``terms=True`` lets us ask each hit which terms matched and
        # use them to build a useful snippet window.
        results = searcher.search(q, limit=limit, terms=True)
        for hit in results:
            summary = hit.get("summary", "")
            title = hit.get("title", "")
            body_stored = hit.get("body", "")  # empty if not stored in schema
            matched_tokens: list[str] = []
            for _, term in hit.matched_terms():
                matched_tokens.append(term)

            snippet = _build_snippet(
                body=body_stored,
                summary=summary,
                title=title,
                matched_tokens=matched_tokens,
                max_chars=240,
            )
            # If the body wasn't stored in the schema (current default —
            # saves index memory on 700MB HDX manuals), fall back to reading
            # the original file from disk so RAG still gets a real snippet
            # window around the matched term rather than the 240-char
            # summary.
            if not snippet or snippet == summary:
                disk_snippet = snippet_from_disk(
                    storage_path=hit.get("id") and _lookup_storage_path(int(hit["id"])),
                    matched_tokens=matched_tokens,
                    max_chars=240,
                )
                if disk_snippet:
                    snippet = disk_snippet
            out.append(
                {
                    "id": int(hit["id"]),
                    "title": title,
                    "vendor": hit.get("vendor", "") or None,
                    "category": hit.get("category", "") or None,
                    "snippet": snippet,
                    "score": float(hit.score),
                }
            )
    return out


_STORAGE_PATH_CACHE: dict[int, Optional[str]] = {}


def _lookup_storage_path(doc_id: int) -> Optional[str]:
    """Resolve a doc_id → storage_path for snippet_from_disk.

    Cached in-process so a single search query doesn't issue N lookups.
    Cache is invalidated on document updates by clear_storage_path_cache().
    """
    if doc_id in _STORAGE_PATH_CACHE:
        return _STORAGE_PATH_CACHE[doc_id]
    from app.models.document import Document  # local import avoids cycles
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(Document, doc_id)
        path = row.storage_path if row else None
    finally:
        db.close()
    _STORAGE_PATH_CACHE[doc_id] = path
    return path


def clear_storage_path_cache() -> None:
    """Drop the in-process cache (call from document update/delete hooks)."""
    _STORAGE_PATH_CACHE.clear()


_SNIPPET_WINDOW = 80


def _build_snippet(
    *,
    body: str,
    summary: str,
    title: str,
    matched_tokens: list[str],
    max_chars: int,
) -> str:
    """Pick the most informative snippet for a search hit.

    Priority:
      1. body window around the first matched token (most useful)
      2. summary (if body isn't stored or no match found in body)
      3. title (last resort)
    """
    if body and matched_tokens:
        body_lower = body.lower()
        for token in matched_tokens:
            idx = body_lower.find(token.lower())
            if idx >= 0:
                start = max(0, idx - _SNIPPET_WINDOW)
                end = min(len(body), idx + len(token) + _SNIPPET_WINDOW)
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(body) else ""
                return (prefix + body[start:end] + suffix).strip()[:max_chars]
    if summary:
        return summary[:max_chars]
    if title:
        return title
    if matched_tokens:
        return "命中关键词: " + ", ".join(matched_tokens)
    return ""


def snippet_from_disk(
    *,
    storage_path: Optional[str],
    matched_tokens: list[str],
    max_chars: int = 240,
) -> str:
    """Fallback: locate a snippet by reading the original file from disk.

    The Whoosh body field isn't ``stored=True`` (to keep the index small
    on hundreds-of-MB HDX manuals). For RAG we still want a real snippet
    around the matched term rather than just the 240-char summary. This
    helper reads up to ``max_chars * 8`` bytes from the original file,
    locates the first matched token, and returns a window around it.

    Failures are swallowed (returns empty string) so the caller can fall
    back to the summary cleanly.
    """
    if not storage_path or storage_path == "pending" or not matched_tokens:
        return ""
    try:
        from app.services.documents import document_storage_path as _resolve

        class _Stub:
            storage_path = storage_path
        p = _resolve(_Stub())
        if not p.exists():
            return ""
        # Read a bounded prefix — never the whole file.
        read_limit = max_chars * 8
        with p.open("rb") as fp:
            raw = fp.read(read_limit)
        # Try the same encodings smart_read uses, then fall back to replace.
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
            try:
                text = raw.decode(enc)
                if enc in ("utf-8", "utf-8-sig") and text.count("\ufffd") > len(text) * 0.02:
                    continue
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        text_lower = text.lower()
        for token in matched_tokens:
            idx = text_lower.find(token.lower())
            if idx >= 0:
                start = max(0, idx - _SNIPPET_WINDOW)
                end = min(len(text), idx + len(token) + _SNIPPET_WINDOW)
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(text) else ""
                return (prefix + text[start:end] + suffix).strip()[:max_chars]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _file_hash_and_size(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as fp:
        while True:
            chunk = fp.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def import_file(*, db: Session, uploaded_by: str, title: str, vendor: Optional[str], category: Optional[str], version_tag: Optional[str], source: Optional[str], file_path: Path, original_filename: str, created_by_user_id: Optional[int] = None) -> Document:
    """Import a local file into the document store + index + Obsidian vault.

    Storage layout
    --------------
    ``{storage_documents_dir}/{hash[:2]}/{hash}_{doc_id}_{filename}``

    * The ``hash`` prefix bucket keeps directory cardinality bounded even
      when millions of documents are stored.
    * The ``hash`` segment gives content-addressed lookup for dedup/diff.
    * The ``doc_id`` segment guarantees a one-row-one-file invariant, so
      deleting one document never collaterally destroys another record's
      on-disk payload (the pre-fix vulnerability identified in the audit).
    * The filename suffix keeps the file readable to a human operator
      inspecting storage directly.
    """
    content_hash, size = _file_hash_and_size(file_path)
    target_dir = ensure_dir(settings.storage_documents_dir / content_hash[:2])
    display_filename = safe_filename(original_filename) or "upload.bin"

    # Insert the row first so we can bind the file path to doc.id. If the
    # subsequent copy fails, the rollback removes the row and we have no
    # orphaned storage.
    doc = Document(
        title=title,
        vendor=vendor,
        category=category,
        version_tag=version_tag,
        filename=display_filename,
        # storage_path is set below after flush() assigns doc.id; using a
        # placeholder keeps the row insert independent of the file write.
        storage_path="pending",
        size_bytes=size,
        content_hash=content_hash,
        summary="",
        source=source,
        uploaded_by=uploaded_by,
        created_by_user_id=created_by_user_id,
    )
    db.add(doc)
    db.flush()  # assigns doc.id
    assert doc.id is not None

    target_name = f"{content_hash}_{doc.id}_{display_filename}"
    # Cap each path component so a 200-char filename can't blow up the path.
    target_name = target_name[:255]
    target = target_dir / target_name

    try:
        shutil.copy2(file_path, target)
        # Only now do we know the final on-disk path; update the row.
        doc.storage_path = str(target.relative_to(settings.storage_documents_dir))

        body_result = smart_read(target)
        body = body_result.content
        doc.summary = _make_summary(body)

        db.commit()
        db.refresh(doc)
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise

    index_document(doc.id, doc.title, body, doc.vendor, doc.category, doc.summary)
    obsidian_service.mirror_document(
        title=doc.title,
        vendor=doc.vendor,
        category=doc.category or settings.obsidian_category_root,
        version_tag=doc.version_tag,
        source=doc.source,
        filename=doc.filename,
        storage_path=str(document_storage_path(doc)),
        uploaded_by=doc.uploaded_by,
        created_at=doc.created_at,
        summary=doc.summary,
        size_bytes=doc.size_bytes,
        doc_id=doc.id,
    )
    return doc


def get_document(db: Session, doc_id: int) -> Optional[Document]:
    return db.get(Document, doc_id)


def document_storage_path(doc: Document) -> Path:
    """Resolve the on-disk path of *doc*, guarded by ``managed_path``.

    The stored ``storage_path`` may be:
    - The relative ``{hash[:2]}/{hash}_{doc_id}_{filename}`` form written
      by ``import_file``.
    - An absolute legacy path from older code that didn't enforce relativity.

    All paths are funnelled through ``managed_path`` so a poisoned row whose
    ``storage_path`` has been edited to point outside the documents tree
    (a theoretical row-level tampering vector) is rejected before any
    subsequent file I/O.
    """
    if not doc.storage_path or doc.storage_path == "pending":
        # Pending rows are mid-upload; no file exists yet.
        raise ValueError("document storage path not finalised")
    stored = Path(doc.storage_path)
    # Refuse paths containing ``..`` segments even after resolve(); a row
    # whose storage_path was tampered with at the DB layer should never
    # escape the documents directory.
    if any(part == ".." for part in stored.parts):
        raise ValueError("document storage path contains traversal segments")
    candidate = stored if stored.is_absolute() else settings.storage_documents_dir / stored
    return managed_path(settings.storage_documents_dir, candidate)


def list_documents(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    vendor: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Return a paginated slice of documents plus the unfiltered total.

    The response is a plain dict matching ``DocumentPage`` so the service
    layer doesn't have to import the Pydantic model (avoids circular
    imports and keeps the model layer thin).
    """
    base = db.query(Document)
    if vendor:
        base = base.filter(Document.vendor == vendor)
    if category:
        base = base.filter(Document.category == category)

    total = base.with_entities(Document.id).count()
    items = (
        base.order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def reindex_document(db: Session, doc_id: int) -> Optional[Document]:
    """Re-read *doc_id* from disk, rebuild its Whoosh entry and summary.

    This is the escape hatch for documents that were imported before the
    container-aware reader existed (e.g. a 711 MB HDX that came in with
    a binary body of U+FFFD replacement characters). The on-disk file
    is unchanged — only the search index and ``summary`` column move.
    """
    doc = db.get(Document, doc_id)
    if not doc:
        return None

    p = document_storage_path(doc)
    if not p.exists():
        raise FileNotFoundError(f"document storage missing: {doc.storage_path}")

    try:
        body_result = smart_read(p)
        body = body_result.content
        doc.summary = _make_summary(body)
        db.refresh(doc)
        db.commit()
        index_document(
            doc.id, doc.title, body, doc.vendor, doc.category, doc.summary,
        )
        # Body content changed → invalidate snippet cache so the next
        # search re-reads from disk.
        _STORAGE_PATH_CACHE.pop(doc.id, None)
        logger.info(
            "reindex: doc {} reindexed, {} entries, encoding={}",
            doc.id, body_result.encoding, body_result.encoding,
        )
        return doc
    except Exception:
        db.rollback()
        raise


def delete_document(db: Session, doc: Document) -> None:
    try:
        os.remove(document_storage_path(doc))
    except FileNotFoundError:
        pass
    except ValueError:
        logger.warning("refusing to delete unmanaged document path for {}", doc.id)
    obsidian_service.remove_document_note(
        title=doc.title,
        vendor=doc.vendor,
        category=doc.category,
    )
    remove_document(doc.id)
    # Drop the in-process snippet cache entry so a follow-up search with
    # the same doc_id (race condition) doesn't try to read a now-missing
    # file.
    _STORAGE_PATH_CACHE.pop(doc.id, None)
    db.delete(doc)
    db.commit()


def update_document(
    db: Session,
    doc: Document,
    *,
    title: Optional[str] = None,
    vendor: Optional[str] = None,
    category: Optional[str] = None,
    version_tag: Optional[str] = None,
    source: Optional[str] = None,
) -> Document:
    """Update document metadata, refresh search index + Obsidian note."""
    old_title = doc.title
    old_vendor = doc.vendor
    old_category = doc.category

    if title is not None:
        doc.title = title
    if vendor is not None:
        doc.vendor = vendor
    if category is not None:
        doc.category = category
    if version_tag is not None:
        doc.version_tag = version_tag
    if source is not None:
        doc.source = source

    db.commit()
    db.refresh(doc)

    # Re-index
    try:
        p = document_storage_path(doc)
        if p.exists():
            body_result = smart_read(p)
            body = body_result.content
        else:
            body = ""
        index_document(doc.id, doc.title, body, doc.vendor, doc.category, doc.summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Re-index failed for doc {}: {}", doc.id, exc)

    # Sync Obsidian note (delete old, write new)
    obsidian_service.remove_document_note(
        title=old_title, vendor=old_vendor, category=old_category,
    )
    obsidian_service.mirror_document(
        title=doc.title,
        vendor=doc.vendor,
        category=doc.category or settings.obsidian_category_root,
        version_tag=doc.version_tag,
        source=doc.source,
        filename=doc.filename,
        storage_path=doc.storage_path,
        uploaded_by=doc.uploaded_by,
        created_at=doc.created_at,
        summary=doc.summary,
        size_bytes=doc.size_bytes,
        doc_id=doc.id,
    )
    # Metadata changed — drop the snippet cache so the next search picks
    # up the new title/vendor/category without serving a stale row.
    _STORAGE_PATH_CACHE.pop(doc.id, None)
    return doc


_SUMMARY_LEN = 240


def _make_summary(body: str) -> str:
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:_SUMMARY_LEN]


def to_html(body: str) -> str:
    return md_lib.markdown(body, extensions=["fenced_code", "tables"])
