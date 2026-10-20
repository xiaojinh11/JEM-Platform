"""Smart file content reader: handles UTF-8, GBK, GB18030, ZIP containers.

Network device exports (Huawei HWICS, Cisco configs, Ruijie logs) are typically
GBK-encoded. Trying to read them as UTF-8 produces mojibake. This module tries
encodings in order and returns the first that decodes cleanly.

ZIP-based containers (HDX / EPUB / DOCX / XLSX / PPTX) are also handled: their
internal text payloads (HTML / XML) are stitched into a single Markdown
document so the frontend can render them as plain text and the search index
can match on the actual content instead of replacement characters.
"""
from __future__ import annotations

from pathlib import Path
from app.utils.container_reader import extract_container

# Try these encodings in order. First successful full-decode wins.
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5", "latin-1")

# Extensions that are *not* plain text — even though some legacy binary
# formats (``.doc``/``.xls``/``.ppt``) contain NUL bytes in their OLE2
# headers, others (``.rtf``/``.csv``) are pure text.  Anything in this set
# is forced to the binary branch so we never render a mis-decoded OLE2
# stream as preview text.
_BINARY_EXTENSIONS = {
    ".doc", ".xls", ".ppt", ".pps",
    ".pdf", ".chm", ".mib", ".bin", ".pkg",
    ".zip", ".7z", ".rar", ".gz", ".tgz", ".tar.gz", ".tar",
    ".hwics", ".hdx", ".mdi", ".xps", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".exe", ".dll", ".so", ".dylib", ".iso",
}


class ReadResult:
    def __init__(self, *, content: str, is_binary: bool, encoding: str | None,
                 note: str = ""):
        self.content = content
        self.is_binary = is_binary
        self.encoding = encoding
        self.note = note

    def __repr__(self) -> str:  # pragma: no cover
        return f"ReadResult(enc={self.encoding!r}, binary={self.is_binary}, len={len(self.content)})"


def _looks_like_text(head: bytes) -> bool:
    """Heuristic: text files contain no null bytes in the first chunk."""
    return b"\x00" not in head


def smart_read(path: Path, max_chars: int = 200_000) -> ReadResult:
    """Read *path* with encoding autodetection.

    Returns a ReadResult describing what was found. Callers should branch on
    `is_binary` to decide whether to display content or prompt for download.
    """
    try:
        with path.open("rb") as fp:
            head = fp.read(512)
    except Exception as exc:  # noqa: BLE001
        return ReadResult(content=f"[读取失败: {exc}]", is_binary=True, encoding=None)

    # ZIP containers (HDX/EPUB/DOCX/XLSX/PPTX) carry the same PK\x03\x04
    # magic. We expand their text payloads into a single Markdown document
    # so the frontend renders them as plain text and the search index has
    # something to match against. This is the primary path for vendor
    # product manuals — without it, every HDX shows up as "binary".
    container = extract_container(path)
    if container is not None and container.markdown.strip():
        return ReadResult(
            content=container.markdown[:max_chars],
            is_binary=False,
            encoding="container",
            note=(
                f"容器文档，索引 {container.entry_count} 个文本条目，"
                f"跳过 {container.skipped_count} 个非文本条目"
                + ("（已截断）" if container.truncated else "")
            ),
        )

    # Force the binary branch for legacy Office formats and other known
    # non-text containers — ``.doc`` is an OLE2 compound document whose
    # header bytes contain no NUL, so the ``_looks_like_text`` heuristic
    # below would otherwise happily decode it as garbled UTF-8.
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        size_kb = (path.stat().st_size / 1024) if path.exists() else 0
        return ReadResult(
            content="",
            is_binary=True,
            encoding="binary",
            note=f"二进制文件（{path.suffix.upper().lstrip('.')} 格式），大小 {size_kb:.1f} KB，无法直接预览",
        )

    if not _looks_like_text(head):
        return ReadResult(
            content="",
            is_binary=True,
            encoding="binary",
            note=f"二进制文件，大小 {path.stat().st_size / 1024:.1f} KB，无法直接预览",
        )

    # Read only a bounded prefix. The caller needs a preview/search body, not
    # the original file in memory.
    read_limit = min(max(max_chars * 4 + 4096, 4096), 4 * 1024 * 1024)
    try:
        with path.open("rb") as fp:
            raw = fp.read(read_limit)
    except Exception as exc:  # noqa: BLE001
        return ReadResult(content=f"[读取失败: {exc}]", is_binary=True, encoding=None)

    for enc in _TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
            # Heuristic: if we got many replacement chars (U+FFFD), try next
            if enc in ("utf-8", "utf-8-sig") and text.count("\ufffd") > len(text) * 0.02:
                continue
            return ReadResult(
                content=text[:max_chars],
                is_binary=False,
                encoding=enc,
            )
        except (UnicodeDecodeError, LookupError):
            continue

    # Fallback: decode with errors='replace' but flag as uncertain
    return ReadResult(
        content=raw.decode("utf-8", errors="replace")[:max_chars],
        is_binary=False,
        encoding="latin-1",
        note="编码不确定，可能存在乱码",
    )
