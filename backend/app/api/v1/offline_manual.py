"""Native viewer endpoints for vendor offline manual packages."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, decode_token
from app.db.session import get_db
from app.models.user import User
from app.schemas.document import OfflineHealthSnapshot
from app.services.documents import document_storage_path, get_document
from app.services.offline_manual import (
    OfflineManualError,
    inspect_offline_manual,
    read_offline_asset,
)
from app.services.offline_health import inspect_offline_health

router = APIRouter()
_COOKIE_NAME = "naw_manual_session"
_SESSION_MINUTES = 10


def validate_offline_session(token: str | None, doc_id: int, db: Session) -> User:
    """Validate a short-lived cookie scoped to one document and active user."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="manual session required")
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub", 0))
        token_doc_id = int(payload.get("doc_id", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid manual session") from exc
    if payload.get("purpose") != "offline_manual" or token_doc_id != doc_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid manual session scope")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


@router.post("/{doc_id}/offline-session")
def create_offline_session(
    doc_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Authenticate once, then let the sandboxed iframe load package assets."""
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        package_path = document_storage_path(doc)
        manifest = inspect_offline_manual(package_path)
    except (OfflineManualError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="该文件不是可用的厂商离线手册包") from exc

    token = create_access_token(
        subject=user.id,
        extra={"purpose": "offline_manual", "doc_id": doc_id},
        expires_minutes=_SESSION_MINUTES,
    )
    base_url = f"{settings.api_v1_prefix}/documents/{doc_id}/offline"
    manifest["base_url"] = base_url
    manifest["home_url"] = f"{base_url}/{quote(manifest['home_path'], safe='/')}"
    try:
        manifest["health"] = inspect_offline_health(package_path).to_dict()
    except Exception:
        # health is best-effort — never block the viewer on it
        manifest["health"] = None
    response = JSONResponse(manifest)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_SESSION_MINUTES * 60,
        httponly=True,
        secure=not settings.debug,
        samesite="strict",
        path=base_url,
    )
    return response


@router.get("/{doc_id}/offline-health", response_model=OfflineHealthSnapshot)
def get_offline_health(
    doc_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Return a vendor-package integrity snapshot for the document.

    The scan walks every HTML / CSS member and verifies that the
    ``href`` / ``src`` / ``url(...)`` references resolve to a real
    archive entry.  Useful for the document list and the viewer so the
    operator can tell vendor-package defects apart from frontend bugs.
    """
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        package_path = document_storage_path(doc)
        health = inspect_offline_health(package_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document storage missing") from exc
    payload = health.to_dict()
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


@router.get("/{doc_id}/offline/{asset_path:path}")
def get_offline_asset(
    doc_id: int,
    asset_path: str,
    db: Session = Depends(get_db),
    manual_session: str | None = Cookie(default=None, alias=_COOKIE_NAME),
):
    """Serve an original HTML/CSS/image member under a sandbox-friendly CSP."""
    validate_offline_session(manual_session, doc_id, db)
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    try:
        package_path = document_storage_path(doc)
        asset = read_offline_asset(package_path, asset_path)
    except (OfflineManualError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="manual asset not found") from exc

    content_type = asset.media_type
    if asset.charset:
        content_type = f"{content_type}; charset={asset.charset}"
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
    }
    if asset.media_type == "text/html":
        headers["Content-Security-Policy"] = (
            "default-src 'self' data: blob:; "
            "script-src 'none'; connect-src 'none'; object-src 'none'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'self'"
        )
    return Response(content=asset.content, headers=headers)


# ── CHM custom-protocol registration ─────────────────────────────────────────
#
# Windows lets users register a custom URL scheme (e.g. ``naw-chm://``) that
# the OS hands off to a chosen executable.  We use that to bridge the
# browser→CHM gap:
#
#   * ``GET /api/v1/documents/{id}/chm-handler.reg`` returns a Windows
#     registry file the operator double-clicks *once*.  That registers
#     ``naw-chm://open?id=<n>`` so the OS invokes ``hh.exe`` with the
#     absolute path of the matching document.
#   * After registration, the "打开本地 CHM" button simply navigates to
#     ``naw-chm://open?id=<n>`` — the browser hands the URL to the OS,
#     the OS hands it to ``hh.exe``, and the operator gets the same
#     window they'd get from a desktop double-click.  No native
#     add-on, no PowerShell dance, no clipboard dance.
#
# The .reg approach is preferred over a Python installer because:
#   * it needs no extra Python packages or pywin32;
#   * it works on any Windows 10/11 with stock hh.exe (which is all
#     of them — hh.exe ships with the OS, not with the app);
#   * the operator can audit the registry file in a text editor
#     before merging it.

import io
import re
import sys
import textwrap


_CHM_REG_PROTOCOL = "naw-chm"


def _windows_doc_path(doc) -> str | None:
    """Return the absolute on-disk path for a CHM document, or ``None``."""
    from pathlib import Path
    from app.services.documents import document_storage_path
    try:
        p = document_storage_path(doc)
    except Exception:
        return None
    return str(Path(p).resolve()) if p else None


@router.get("/{doc_id}/chm-handler.reg")
def chm_handler_registry(
    doc_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Return a ``.reg`` file that wires ``naw-chm://`` to ``hh.exe``.

    The handler runs ``hh.exe "<absolute-path-to-doc>"`` so the OS
    pops up the regular Microsoft HTML Help viewer.  ``%1`` is the
    URL the operator clicked (we never use it — the path is
    embedded so the click resolves to a specific document without
    a round-trip).

    IMPORTANT: this endpoint requires authentication.  The .reg is
    also signed off in the file itself: every line is commented
    except the HKCU keys, so merging it touches only the current
    user hive, not HKLM.
    """
    doc = get_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    if (doc.filename or "").lower().endswith(".chm") is False:
        raise HTTPException(status_code=400, detail="document is not a CHM")
    abs_path = _windows_doc_path(doc)
    if not abs_path:
        raise HTTPException(status_code=404, detail="chm file not found on disk")
    if sys.platform != "win32":
        raise HTTPException(
            status_code=400,
            detail="custom-protocol registration is Windows-only",
        )

    # Escape backslashes for the .reg file (which is Windows-INI-ish
    # but treats backslashes literally).  hh.exe accepts forward
    # slashes too, but we use backslashes for parity with Explorer.
    win_path = abs_path.replace("\\", "\\\\")

    reg = textwrap.dedent(
        f"""\
        Windows Registry Editor Version 5.00

        ; ---------------------------------------------------------------
        ;  NAW — auto-document CHM handler
        ;
        ;  Registers the ``naw-chm://`` URL scheme so the browser can
        ;  hand a click straight to hh.exe with the document's absolute
        ;  path.  This file was generated by the NAW backend for
        ;  document id={doc_id} (filename={doc.filename!r}).
        ;
        ;  All keys live under HKCU so no admin rights are required
        ;  to merge this file.  To uninstall, delete the keys under
        ;  ``HKEY_CURRENT_USER\\Software\\Classes\\naw-chm``.
        ; ---------------------------------------------------------------

        [HKEY_CURRENT_USER\\Software\\Classes\\naw-chm]
        @="URL:NAW auto-document CHM handler"
        "URL Protocol"=""

        [HKEY_CURRENT_USER\\Software\\Classes\\naw-chm\\DefaultIcon]
        @="hh.exe,0"

        [HKEY_CURRENT_USER\\Software\\Classes\\naw-chm\\shell]

        [HKEY_CURRENT_USER\\Software\\Classes\\naw-chm\\shell\\open]

        [HKEY_CURRENT_USER\\Software\\Classes\\naw-chm\\shell\\open\\command]
        @="\\"hh.exe\\" \\"{win_path}\\""
        """
    )

    headers = {
        "Content-Type": "application/x-registry-edit; charset=utf-8",
        "Content-Disposition": (
            f'attachment; filename="naw-chm-handler-{doc_id}.reg"'
        ),
        "Cache-Control": "no-store",
    }
    return Response(content=reg, headers=headers)


@router.get("/{doc_id}/chm-launch-url")
def chm_launch_url(
    doc_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Return the ``naw-chm://`` URL the browser should navigate to.

    Frontend uses this to decide whether the OS-level handler is
    registered: if the GET succeeds the URL is well-formed; the
    actual ``hh.exe`` invocation only happens after the operator
    has merged the .reg from the related endpoint.
    """
    doc = get_document(db, doc_id)
    if not doc or not (doc.filename or "").lower().endswith(".chm"):
        raise HTTPException(status_code=404, detail="document not found")
    url = f"{_CHM_REG_PROTOCOL}://open?id={doc_id}"
    return JSONResponse({"url": url})
