"""Obsidian vault integration — bidirectional links + embedded content + graph support.

Vault layout
────────────
E:/opus1/opus/
  index.md                          ← auto-generated master index (overwritten on every upload)
  产品手册/
    <vendor>/
      <title>.md                    ← full note with YAML frontmatter + embedded content
      <title>.assets/
        <original_filename>         ← binary source backup (pdf/docx/etc.)

Graph strategy
──────────────
Each note embeds:
  - The full text for .md/.txt/.cfg/.log files
  - A file:// link for binary files (PDF/Word/etc.)
  - Backlink to [[产品手册]] (category hub)
  - Backlink to [[<vendor>]] (vendor hub)
  - Tags: #产品手册 #厂商/<vendor> #v<version>

The category hub (产品手册.md) and vendor hub (<vendor>.md) files are maintained
automatically so Obsidian's native graph view shows connections immediately.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import logger

# ── filesystem helpers ────────────────────────────────────────────────────────

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_MULTI_DASH = re.compile(r'-+')


def _slug(name: str) -> str:
    """Safe, filesystem-safe, URL-friendly slug."""
    cleaned = _BAD_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(" .")
    return _MULTI_DASH.sub("-", cleaned) or "untitled"


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── vault paths ──────────────────────────────────────────────────────────────

def _vault() -> Optional[Path]:
    if not settings.obsidian_enabled:
        return None
    p = Path(settings.obsidian_vault_dir).expanduser().resolve()
    if not p.exists():
        return None
    return p


def _category_dir(vault: Path, category: str) -> Path:
    return _ensure_dir(vault / _slug(category))


def _vendor_dir(vault: Path, category: str, vendor: str) -> Path:
    return _ensure_dir(_category_dir(vault, category) / _slug(vendor))


# ── content extraction ────────────────────────────────────────────────────────

# Extensions treated as text (full content embedded)
_TEXT_EXTS = {".md", ".txt", ".log", ".cfg", ".conf", ".ini", ".yaml", ".yml",
              ".xml", ".json", ".py", ".sh", ".bat", ".ps1", ".sql", ".csv",
              ".mib", ".hwics"}


def _is_text(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in _TEXT_EXTS


def _read_content(file_path: Path, max_chars: int = 80_000) -> str:
    """Read file as text with a hard size cap, fallback to binary-size marker.

    Reads the file in bounded chunks rather than via ``read_bytes()`` so that
    a multi-gigabyte firmware .bin accidentally routed here does not OOM the
    worker. The first chunk is enough to detect binary content; we stop as
    soon as we hit ``max_chars``.
    """
    _BINARY_PEEK = 512
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return f"[读取失败: {exc}]"

    try:
        with file_path.open("rb") as fp:
            head = fp.read(_BINARY_PEEK)
    except OSError as exc:
        return f"[读取失败: {exc}]"

    if b"\x00" in head:
        return f"[二进制文件，大小: {size / 1024:.1f} KB]"

    # Decode incrementally and stop at max_chars * 4 bytes (worst case UTF-8).
    raw_max = max_chars * 4 + 4096
    try:
        with file_path.open("rb") as fp:
            chunks: list[bytes] = []
            consumed = 0
            while True:
                chunk = fp.read(64 * 1024)
                if not chunk:
                    break
                consumed += len(chunk)
                chunks.append(chunk)
                if consumed >= raw_max:
                    break
        raw = b"".join(chunks)
    except OSError as exc:
        return f"[读取失败: {exc}]"

    text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n> ... (内容过长，已截断前 {max_chars} 字符)"
    return text


# ── note generation ──────────────────────────────────────────────────────────

def _yaml_escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')


def _make_frontmatter(
    title: str,
    vendor: str,
    category: str,
    version_tag: Optional[str],
    source: Optional[str],
    filename: str,
    uploaded_by: str,
    created_at: datetime,
    size_kb: str,
    doc_id: int,
) -> str:
    return "\n".join([
        "---",
        f"id: {doc_id}",
        f"title: \"{_yaml_escape(title)}\"",
        f"vendor: \"{_yaml_escape(vendor)}\"",
        f"category: \"{_yaml_escape(category)}\"",
        f"version: \"{_yaml_escape(version_tag or '')}\"",
        f"uploaded_by: \"{_yaml_escape(uploaded_by)}\"",
        f"uploaded_at: \"{created_at.strftime('%Y-%m-%d %H:%M:%S')}\"",
        f"source_file: \"{_yaml_escape(filename)}\"",
        f"size: \"{size_kb}\"",
        f"tags: [{_tag_yaml(vendor, category, version_tag)}]",
        "---",
    ])


def _tag_yaml(vendor: str, category: str, version: Optional[str]) -> str:
    parts = [f'"{category}"', f'"厂商/{vendor}"']
    if version:
        parts.append(f'"v{version}"')
    return ", ".join(parts)


def _make_body(
    title: str,
    vendor: str,
    category: str,
    version_tag: Optional[str],
    source: Optional[str],
    uploaded_by: str,
    uploaded_at: datetime,
    size_kb: str,
    content: str,
    source_path: Optional[str],
    original_filename: str,
) -> str:
    ts = uploaded_at.strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join([
        f"# {title}",
        "",
        "| 属性 | 值 |",
        "|------|----|",
        f"| 厂商 | [[{vendor}]] |",
        f"| 分类 | [[{category}]] |",
        f"| 版本 | {version_tag or '—'} |",
        f"| 来源 | {source or '—'} |",
        f"| 上传人 | {uploaded_by} |",
        f"| 上传时间 | {ts} |",
        f"| 文件大小 | {size_kb} |",
        "",
        "## 文档内容",
        "",
        content,
        "",
        "## 原始文件",
        "",
        f"- 文件名：`{original_filename}`",
        f"- 存储路径：`{source_path or '—'}`",
        "",
    ])


# ── hub notes (category index + vendor index) ────────────────────────────────

def _make_hub_note(vault: Path, name: str, hub_type: str, backlink: Optional[str]) -> Path:
    """Create / update a hub note (category root or vendor folder index)."""
    path = _ensure_dir(vault / _slug(name)) / "index.md"
    links = ""
    if backlink:
        links = f"\n\n> 所属：[[{backlink}]]\n"
    path.write_text(
        f"# {name}\n\n"
        f"**类型**：{hub_type}\n\n"
        f"此文件夹由 Network Automation Workbench 自动管理。"
        f"{links}\n",
        encoding="utf-8",
    )
    return path


# ── public API ───────────────────────────────────────────────────────────────

def mirror_document(
    *,
    title: str,
    vendor: str,
    category: str,
    version_tag: Optional[str],
    source: Optional[str],
    filename: str,
    storage_path: str,
    uploaded_by: str,
    created_at: datetime,
    summary: Optional[str],
    size_bytes: int,
    doc_id: int,
) -> Optional[Path]:
    """Write a full Obsidian note for the given document.

    Returns the path of the created note, or None when mirroring is disabled.
    All errors are swallowed — the upload pipeline must never fail because of vault writes.
    """
    try:
        vault = _vault()
        if vault is None:
            logger.debug("Obsidian vault disabled or not found")
            return None

        vdir = _vendor_dir(vault, category, vendor)
        stem = _slug(title)
        note_path = _ensure_dir(vdir) / f"{stem}.md"

        # avoid overwriting existing notes with same title
        suffix = 1
        while note_path.exists():
            note_path = vdir / f"{stem} ({suffix}).md"
            suffix += 1

        # ── extract content from source file ──────────────────────────────
        src = Path(storage_path)
        size_kb = f"{size_bytes / 1024:.1f} KB"
        if src.exists():
            if _is_text(filename):
                content = _read_content(src)
            else:
                # binary: embed file link + size
                content = (
                    f"> 📎 此文档为二进制格式（{size_kb}），"
                    f"原始文件保存在：\n"
                    f"> `{src.as_posix()}`\n\n"
                    f"> **提示**：在 Obsidian 中按住 Ctrl 点击上方路径可直接打开文件所在位置。"
                )
        else:
            content = f"_⚠️ 源文件未找到：`{storage_path}`_"

        # ── build note ────────────────────────────────────────────────────
        fm = _make_frontmatter(
            title=title, vendor=vendor, category=category,
            version_tag=version_tag, source=source,
            filename=filename, uploaded_by=uploaded_by,
            created_at=created_at, size_kb=size_kb, doc_id=doc_id,
        )
        body = _make_body(
            title=title, vendor=vendor, category=category,
            version_tag=version_tag, source=source,
            uploaded_by=uploaded_by, uploaded_at=created_at,
            size_kb=size_kb, content=content,
            source_path=storage_path, original_filename=filename,
        )

        note_path.write_text(fm + "\n" + body, encoding="utf-8")
        logger.info("Obsidian note: {}", note_path)

        # ── maintain hub notes (graph requires indexed connections) ─────────
        _make_hub_note(vault, category, "分类", None)
        _make_hub_note(vault, vendor, "厂商", category)

        # ── update vault index ────────────────────────────────────────────
        _update_vault_index(vault, category, vendor, title, doc_id)

        return note_path

    except Exception as exc:  # noqa: BLE001
        logger.warning("Obsidian mirror failed for {}: {}", title, exc)
        return None


def remove_document_note(
    *,
    title: str,
    vendor: Optional[str],
    category: Optional[str],
    doc_id: Optional[int] = None,
) -> bool:
    """Remove the vault note for a deleted document (best-effort)."""
    try:
        vault = _vault()
        if vault is None:
            return False

        if vendor and category:
            vdir = _vendor_dir(vault, category, vendor)
            stem = _slug(title)
            for candidate in vdir.glob(f"{stem}*.md"):
                candidate.unlink()
                logger.info("Removed Obsidian note: {}", candidate)

        # Also strip entry from vault index
        _strip_vault_index(vault, title, vendor, category)

        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Obsidian note removal failed: {}", exc)
        return False


def update_document_note(
    *,
    title: str,
    new_title: Optional[str],
    vendor: Optional[str],
    category: Optional[str],
    version_tag: Optional[str],
    source: Optional[str],
    storage_path: str,
    uploaded_by: str,
    created_at: datetime,
    summary: Optional[str],
    size_bytes: int,
    doc_id: int,
) -> Optional[Path]:
    """Re-write the vault note after a document update (rename, metadata change)."""
    # Remove old note first, then write new one
    remove_document_note(title=title, vendor=vendor, category=category, doc_id=doc_id)
    if new_title:
        title = new_title
    return mirror_document(
        title=title, vendor=vendor or "其他",
        category=category or settings.obsidian_category_root,
        version_tag=version_tag, source=source,
        filename=title + ".md",  # updated filename
        storage_path=storage_path, uploaded_by=uploaded_by,
        created_at=created_at, summary=summary,
        size_bytes=size_bytes, doc_id=doc_id,
    )


def _update_vault_index(
    vault: Path,
    category: str,
    vendor: str,
    doc_title: str,
    doc_id: int,
) -> None:
    """Append or update the vault root index.md with a link to the new document."""
    index_path = vault / "index.md"
    stem = _slug(doc_title)
    link = f"- [{doc_title}]({_slug(category)}/{_slug(vendor)}/{stem}.md)"
    doc_link = f"[[{_slug(category)}/{_slug(vendor)}/{stem}.md|{doc_title}]]"

    if index_path.exists():
        lines = index_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["# 手册库索引", "", f"> 由 Network Automation Workbench 自动维护", ""]

    # deduplicate — remove old entry for this doc_title
    marker = f"[{doc_title}]("
    cleaned = [l for l in lines if
               not (l.strip().startswith(f"- [{doc_title}]") or
                    marker in l)]
    # insert before last empty line cluster
    insert_at = len(cleaned)
    for i in range(len(cleaned) - 1, -1, -1):
        if cleaned[i].strip():
            insert_at = i + 1
            break
    rel = f"{_slug(category)}/{_slug(vendor)}/{stem}.md"
    cleaned.insert(insert_at, f"- [{doc_title}]({rel})")

    index_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")


def _strip_vault_index(
    vault: Path,
    doc_title: str,
    vendor: Optional[str],
    category: Optional[str],
) -> None:
    """Remove all entries for a doc_title from the vault root index.md."""
    index_path = vault / "index.md"
    if not index_path.exists():
        return
    lines = index_path.read_text(encoding="utf-8").splitlines()
    cleaned = []
    for line in lines:
        if line.startswith(f"- [{doc_title}]"):
            continue
        cleaned.append(line)
    # Collapse multiple blank lines that may result
    final: list[str] = []
    blank = False
    for line in cleaned:
        if line.strip():
            final.append(line)
            blank = False
        elif not blank:
            final.append(line)
            blank = True
    index_path.write_text("\n".join(final) + "\n", encoding="utf-8")
