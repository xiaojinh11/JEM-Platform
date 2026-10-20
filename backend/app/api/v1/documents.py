"""Document management + search."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, require_operator
from app.db.session import get_db
from app.models.user import User
from app.schemas.document import (
    DocumentDetail,
    DocumentOut,
    DocumentPage,
    DocumentSearchResponse,
    DocumentSearchHit,
    DocumentUpdate,
    OfflineHealthSnapshot,
)
from app.services.documents import delete_document, document_storage_path, get_document, import_file, list_documents, reindex_document, search, update_document
from app.services.offline_health import inspect_offline_health
from app.utils.file_reader import smart_read
from app.core.config import settings


router = APIRouter()

_ALLOWED_DOCUMENT_EXTENSIONS = {
    ".md", ".txt", ".log", ".csv", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".pdf", ".html", ".htm", ".mhtml", ".mdi", ".xps",
    ".epub", ".hwics", ".hdx", ".bin", ".tar", ".pkg", ".zip", ".7z",
    ".rar", ".gz", ".tgz", ".tar.gz", ".cfg", ".dat", ".ini", ".conf",
    ".yaml", ".yml", ".xml", ".json", ".py", ".sh", ".chm", ".mib",
}


def validate_document_filename(filename: str) -> None:
    """Reject file types outside the documented server-side allowlist."""
    lower = filename.lower()
    suffix = ".tar.gz" if lower.endswith(".tar.gz") else Path(lower).suffix
    if suffix not in _ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"不支持的文件格式：{suffix or '(无扩展名)'}",
        )


def _safe_health(doc) -> Optional[OfflineHealthSnapshot]:
    """Compute a package health snapshot for a vendor offline manual.

    Returns ``None`` for non-``.hdx`` documents. Any exception inside
    the scanner is swallowed so the document detail endpoint never
    fails because of a corrupt package — the viewer surface stays
    responsive even when the archive itself is half-broken.
    """
    if not doc or not doc.filename or not doc.filename.lower().endswith(".hdx"):
        return None
    try:
        package_path = document_storage_path(doc)
        payload = inspect_offline_health(package_path).to_dict()
        return OfflineHealthSnapshot(
            score=payload["score"],
            grade=payload["grade"],
            total_members=payload["total_members"],
            html_members=payload["html_members"],
            css_members=payload["css_members"],
            references_total=payload["references_total"],
            references_missing=payload["references_missing"],
            references_case_only=payload["references_case_only"],
            references_by_kind=payload["references_by_kind"],
            missing_by_kind=payload["missing_by_kind"],
            missing_targets=payload["missing_targets"],
            case_only_targets=payload["case_only_targets"],
            top_missing_sources=payload["top_missing_sources"],
        )
    except Exception:
        return None


@router.get("", response_model=DocumentPage)
def list_docs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    vendor: str | None = Query(None, max_length=32),
    category: str | None = Query(None, max_length=64),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return list_documents(db, limit=limit, offset=offset, vendor=vendor, category=category)


@router.get("/search", response_model=DocumentSearchResponse)
def search_docs(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    hits = search(q, limit=limit)
    return DocumentSearchResponse(
        query=q,
        total=len(hits),
        hits=[DocumentSearchHit(**h) for h in hits],
    )


@router.get("/{doc_id}", response_model=DocumentDetail)
def get_doc(doc_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")

    # Use encoding-aware reader so GBK-encoded HWICS / config files render
    # correctly instead of mojibake. Binary files return is_binary=True so
    # the frontend can prompt for download.
    content = ""
    is_binary = False
    encoding: Optional[str] = None
    try:
        p = document_storage_path(doc)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="文档存储路径无效") from exc
    if p.exists():
        result = smart_read(p)
        content = result.content
        is_binary = result.is_binary
        encoding = result.encoding
    else:
        content = "(源文件不存在)"

    return DocumentDetail(
        id=doc.id,
        title=doc.title,
        vendor=doc.vendor,
        category=doc.category,
        version_tag=doc.version_tag,
        filename=doc.filename,
        size_bytes=doc.size_bytes,
        content_hash=doc.content_hash,
        summary=doc.summary,
        source=doc.source,
        uploaded_by=doc.uploaded_by,
        created_at=doc.created_at,
        # Do not expose the server's absolute filesystem path to the browser.
        storage_path=None,
        content=content,
        is_binary=is_binary,
        encoding=encoding,
        offline_health=_safe_health(doc),
    )


@router.post("", response_model=DocumentOut, status_code=201)
def upload_doc(
    file: UploadFile,
    title: str = Query(..., min_length=1, max_length=256),
    vendor: str | None = Query(None, max_length=32),
    category: str | None = Query(None, description="分类，固定为 产品手册"),
    version_tag: str | None = Query(None, max_length=64),
    source: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    _user: User = Depends(require_operator),
):
    validate_document_filename(file.filename or title)
    # Category is locked — force the canonical value
    category = category or settings.obsidian_category_root or "产品手册"

    # Hard cap on the upload body. 500 MB matches the largest practical
    # Huawei HDX in the wild and is large enough for firmware images, but
    # not so large that a malicious client can fill the disk or that a
    # worker must juggle multi-gigabyte payloads. The previous 2 GB cap
    # was unbounded enough that a single upload could exhaust worker
    # memory before smart_read's bounded read kicked in.
    MAX_BYTES = 500 * 1024 * 1024
    if file.size is not None and file.size > MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 500MB 上限（{file.size} bytes），请压缩或分卷",
        )

    # Stream the upload to a temp file in 1 MB chunks instead of
    # buffering the whole payload into memory (which would OOM on a
    # 678 MB firmware upgrade).
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
            written = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                written += len(chunk)
                if written > MAX_BYTES:
                    # Bail out mid-stream and clean up
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过 500MB 上限，已写入 {written} bytes",
                    )
        assert tmp_path is not None
        doc = import_file(
            db=db,
            uploaded_by=_user.username,
            title=title,
            vendor=vendor,
            category=category,
            version_tag=version_tag,
            source=source,
            file_path=tmp_path,
            original_filename=file.filename or title,
            created_by_user_id=_user.id,
        )
        # Defer heavy parse (smart_read + Whoosh + Obsidian mirror) to the
        # worker. The HTTP response now only includes the row, not the
        # parsed body, so a 200 MB HDX doesn't blow the request thread.
        # If the broker is unavailable the row still exists; the operator
        # can recover via ``POST /documents/{id}/reindex``.
        try:
            from app.workers.document_tasks import parse_document

            parse_document.delay(doc.id)
        except Exception as exc:  # broker unreachable, etc.
            import logging
            logging.getLogger("naw.documents").warning(
                "parse_document dispatch failed for doc %s: %s", doc.id, exc,
            )
        return doc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


@router.patch("/{doc_id}", response_model=DocumentOut)
def patch_doc(
    doc_id: int,
    body: DocumentUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(require_operator),
):
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    # Force canonical category — ignore whatever client sent
    forced_category = settings.obsidian_category_root or "产品手册"
    updated = update_document(
        db=db,
        doc=doc,
        title=body.title,
        vendor=body.vendor,
        category=forced_category,
        version_tag=body.version_tag,
        source=body.source,
    )
    return updated


@router.delete("/{doc_id}", status_code=204)
def remove_doc(doc_id: int, db: Session = Depends(get_db), _user: User = Depends(require_admin)):
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    delete_document(db, doc)
    return None


@router.post("/{doc_id}/reindex", response_model=DocumentOut)
def reindex_doc(doc_id: int, db: Session = Depends(get_db), _user: User = Depends(require_operator)):
    """Schedule a background re-parse of the on-disk file.

    Useful for documents uploaded before the container-aware reader
    existed (e.g. a 711 MB Huawei HDX whose body is currently a wall of
    replacement characters). The on-disk file is untouched; only the
    Whoosh entry and the ``summary`` column are refreshed. The actual
    parse runs in the Celery worker so the HTTP response returns
    immediately with ``index_status='pending'``.
    """
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    doc.index_status = "pending"
    doc.index_error = None
    db.commit()
    db.refresh(doc)
    try:
        from app.workers.document_tasks import parse_document

        parse_document.delay(doc_id)
    except Exception as exc:
        # If the broker is unreachable, fall back to inline reindex so
        # the operator still gets a working index for small files. Large
        # files will simply fail with a clear error in index_error.
        from app.core.logging import logger

        logger.warning("reindex: broker unavailable, falling back inline: {}", exc)
        result = reindex_document(db, doc_id)
        if result is not None:
            doc = result
    return doc


@router.get("/{doc_id}/download")
def download_doc(doc_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Stream the original file back to the browser for download.

    The ``Content-Disposition: attachment`` header is what tells the
    browser (and external download managers such as IDM) to save the
    payload to disk instead of letting the page handle it.  Use this
    endpoint when the user clicks the explicit *Download* button.
    """
    from fastapi.responses import FileResponse
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    try:
        p = document_storage_path(doc)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="文档存储路径无效") from exc
    if not p.exists():
        raise HTTPException(status_code=410, detail="源文件已丢失")
    # Use original filename when present, fall back to stored name.
    download_name = doc.filename or p.name
    return FileResponse(
        path=str(p),
        filename=download_name,
        media_type="application/octet-stream",
    )


@router.get("/{doc_id}/raw")
def raw_doc(doc_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Stream the original file *inline* (no attachment header).

    Used by DocxViewer to pull the blob for in-browser parsing.  We
    must return ``Content-Disposition: inline`` so that external
    download managers (IDM / 迅雷) don't hijack the response and pop
    a "new download task" dialog — those tools only intercept
    ``attachment``-flavoured responses, so flipping the disposition
    lets the JS ``fetch`` actually receive the bytes.
    """
    from fastapi.responses import FileResponse
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    try:
        p = document_storage_path(doc)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="文档存储路径无效") from exc
    if not p.exists():
        raise HTTPException(status_code=410, detail="源文件已丢失")
    # Pick a MIME type from the extension so the browser has a hint,
    # but default to octet-stream for unknown formats.
    suffix = p.suffix.lower().lstrip('.')
    mime = _INLINE_MIME.get(suffix, "application/octet-stream")
    return FileResponse(
        path=str(p),
        media_type=mime,
        # Setting ``Content-Disposition`` to ``inline`` keeps IDM /
        # 迅雷 from snatching the response.
        headers={"Content-Disposition": "inline"},
    )


# Used by ``raw_doc`` so the browser sees a sensible MIME type while
# still treating the response as inline.
_INLINE_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}
