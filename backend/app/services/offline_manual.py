"""Read vendor offline manuals without flattening their HTML into Markdown."""
from __future__ import annotations

import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

_MAX_PROFILE_BYTES = 1024 * 1024
_MAX_NAVI_BYTES = 12 * 1024 * 1024
_MAX_ASSET_BYTES = 64 * 1024 * 1024
_MAX_TOC_NODES = 50_000
_MAX_TOC_DEPTH = 64
_CHARSET_RE = re.compile(br"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", re.I)


class OfflineManualError(ValueError):
    """The archive is not a supported or safe offline manual package."""


@dataclass(frozen=True)
class OfflineAsset:
    content: bytes
    media_type: str
    charset: str | None = None


def _safe_member_path(member: str) -> str:
    candidate = member.replace("\\", "/").strip()
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        raise OfflineManualError("invalid offline-manual path")
    normalized = posixpath.normpath(candidate)
    parts = PurePosixPath(normalized).parts
    if normalized == ".." or any(part == ".." for part in parts):
        raise OfflineManualError("offline-manual path traversal detected")
    return normalized


def _read_member_limited(
    archive: zipfile.ZipFile,
    member: str,
    *,
    max_bytes: int,
) -> bytes:
    safe_member = _safe_member_path(member)
    try:
        info = archive.getinfo(safe_member)
    except KeyError as exc:
        raise OfflineManualError(f"offline-manual entry not found: {safe_member}") from exc
    if info.is_dir() or info.file_size > max_bytes:
        raise OfflineManualError(f"offline-manual entry exceeds limit: {safe_member}")
    with archive.open(info) as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise OfflineManualError(f"offline-manual entry exceeds limit: {safe_member}")
    return content


def _child_text(root: ElementTree.Element, name: str) -> str:
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


def _topic_tree(root: ElementTree.Element, navi_path: str) -> list[dict]:
    base = PurePosixPath(navi_path).parent
    count = 0

    def convert(element: ElementTree.Element, depth: int) -> dict:
        nonlocal count
        count += 1
        if count > _MAX_TOC_NODES or depth > _MAX_TOC_DEPTH:
            raise OfflineManualError("offline-manual navigation exceeds limit")

        url = (element.attrib.get("url") or "").strip()
        full_path = _safe_member_path(str(base / url)) if url else None
        children = [
            convert(child, depth + 1)
            for child in element
            if child.tag.rsplit("}", 1)[-1] == "topic"
        ]
        return {
            "id": element.attrib.get("id") or f"node-{count}",
            "label": (element.attrib.get("txt") or "未命名主题").strip(),
            "path": full_path,
            "children": children,
        }

    return [
        convert(child, 1)
        for child in root
        if child.tag.rsplit("}", 1)[-1] == "topic"
    ]


def inspect_offline_manual(path: Path) -> dict:
    """Return package metadata and the original hierarchical navigation."""
    try:
        with zipfile.ZipFile(path) as archive:
            profile_raw = _read_member_limited(
                archive, "profile.xml", max_bytes=_MAX_PROFILE_BYTES
            )
            profile = ElementTree.fromstring(profile_raw)
            navi_path = _safe_member_path(_child_text(profile, "navi"))
            home_path = _safe_member_path(_child_text(profile, "homePage"))
            navi_raw = _read_member_limited(
                archive, navi_path, max_bytes=_MAX_NAVI_BYTES
            )
            navi = ElementTree.fromstring(navi_raw)
            tree = _topic_tree(navi, navi_path)
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise OfflineManualError("invalid offline-manual package") from exc

    return {
        "title": _child_text(profile, "libName") or path.stem,
        "product_type": _child_text(profile, "productType") or None,
        "product_version": _child_text(profile, "productVersion") or None,
        "document_version": _child_text(profile, "libVersion") or None,
        "issue_date": _child_text(profile, "issueDate") or None,
        "language": _child_text(profile, "language") or None,
        "home_path": home_path,
        "tree": tree,
    }


def read_offline_asset(path: Path, member: str) -> OfflineAsset:
    """Read one original HTML/CSS/image asset from an offline package."""
    safe_member = _safe_member_path(member)
    if not safe_member.startswith("resources/"):
        raise OfflineManualError("only package resources may be served")
    try:
        with zipfile.ZipFile(path) as archive:
            content = _read_member_limited(
                archive, safe_member, max_bytes=_MAX_ASSET_BYTES
            )
    except zipfile.BadZipFile as exc:
        raise OfflineManualError("invalid offline-manual package") from exc

    media_type = mimetypes.guess_type(safe_member)[0] or "application/octet-stream"
    charset = None
    if media_type in {"text/html", "text/css", "application/javascript", "text/javascript"}:
        match = _CHARSET_RE.search(content[:4096])
        charset = match.group(1).decode("ascii", errors="ignore") if match else "utf-8"
    return OfflineAsset(content=content, media_type=media_type, charset=charset)
