"""Container-aware text extractor.

HDX / EPUB / DOCX / XLSX / PPTX are all ZIP containers with the `PK\\x03\\x04`
magic header. The naive text reader in `file_reader.py` treats them as binary
because the ZIP local-file header contains null bytes, so the frontend shows
"binary file" and the search index ends up full of \\ufffd replacement chars.

This module recognizes ZIP containers, walks them, and stitches the textual
parts (HTML / XML / TXT) into a single Markdown document with a TOC of the
file paths. It is streaming-friendly: each entry is decoded incrementally
and we stop once we hit a hard cap so a 711 MB HDX doesn't OOM the worker.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from app.core.logging import logger

# These extensions are treated as "text-ish" payloads when extracting
# from inside a container. We deliberately skip binary media (images,
# fonts, archives nested inside) so we don't waste memory or pollute
# the index with base64 garbage.
_TEXT_EXTS = (
    ".html", ".htm", ".xhtml", ".xml", ".opf", ".ncx",
    ".txt", ".md", ".markdown", ".rst",
)

# Hard caps to keep memory bounded when expanding a 700 MB+ HDX.
_MAX_TOTAL_CHARS = 2_000_000       # ~4-10 MB UTF-8 — enough to be useful
_MAX_PER_ENTRY_CHARS = 80_000       # any single HTML chunk above this gets truncated
_MAX_PER_ENTRY_BYTES = _MAX_PER_ENTRY_CHARS * 4 + 4096
_MAX_ENTRIES = 1500                 # safety net: don't visit more than N entries
# Defence against zip bombs: refuse entries whose declared uncompressed size
# exceeds this many times their compressed size. 100x is a very generous bound
# for legit text content; real text rarely compresses beyond 5–10x.
_MAX_COMPRESSION_RATIO = 100
# Minimum compressed bytes before the ratio check applies. Tiny entries
# naturally have huge apparent ratios (a 100-byte string compresses to
# almost nothing) but can never actually blow up memory.
_MIN_COMPRESS_SIZE_FOR_RATIO = 4 * 1024
# Cap the cumulative *compressed* bytes we will hand to zf.read before
# truncation. This stops an attacker from forcing us to inflate a huge
# archived file even if their declared sizes are small.
_MAX_TOTAL_COMPRESSED_BYTES = 64 * 1024 * 1024   # 64 MiB
_SKIP_DIR_PREFIXES = ("__MACOSX/", ".git/", "node_modules/")
# A small set of very common CSS/JS file names that some Huawei manuals keep
# inside the container as standalone entries. Including them adds noise without
# meaningful searchable text.
_NOISE_BASENAMES = {"stylesheet.css", "style.css", "normalize.css"}


@dataclass
class ContainerText:
    markdown: str
    entry_count: int
    skipped_count: int
    truncated: bool


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")


def _strip_html(html: str) -> str:
    """HTML to plain text — good enough for indexing and preview.

    Not a full parser, but a sensible approximation: drop tags, normalize
    common entities, collapse whitespace, and preserve block boundaries
    so paragraphs survive.
    """
    # Insert a newline before block-level tags so we get paragraph breaks.
    html = re.sub(
        r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|td|th|pre|section|article|header|footer)[^>]*>",
        "\n",
        html,
        flags=re.IGNORECASE,
    )
    text = _HTML_TAG_RE.sub(" ", html)
    text = _HTML_ENTITY_RE.sub(
        lambda m: {
            "nbsp": " ",
            "amp": "&",
            "lt": "<",
            "gt": ">",
            "quot": '"',
        }.get(m.group(1), m.group(0))
        if not m.group(1).startswith("#")
        else m.group(0),
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def _iter_text_entries(zf: zipfile.ZipFile) -> Iterable[zipfile.ZipInfo]:
    """Yield at most ``_MAX_ENTRIES`` text entries without extra copies."""
    seen = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if any(name.startswith(p) for p in _SKIP_DIR_PREFIXES):
            continue
        lower = name.lower()
        if any(lower.endswith(ext) for ext in _TEXT_EXTS):
            yield info
            seen += 1
            if seen >= _MAX_ENTRIES:
                return


def extract_container(path: Path) -> Optional[ContainerText]:
    """Return extracted Markdown text if *path* is a ZIP-like container, else None."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic != b"PK\x03\x04":
            return None
    except Exception:
        return None

    parts: List[str] = []
    total = 0
    truncated = False
    entry_count = 0
    skipped_count = 0
    compressed_consumed = 0

    try:
        with zipfile.ZipFile(path) as zf:
            total_entries = len(zf.infolist())
            for info in _iter_text_entries(zf):
                if total >= _MAX_TOTAL_CHARS:
                    truncated = True
                    break

                base = Path(info.filename).name.lower()
                if base in _NOISE_BASENAMES:
                    continue

                # Defence against zip bombs: refuse entries whose declared
                # uncompressed size vastly exceeds their compressed size.
                # ``info.compress_size`` is 0 for STORED entries (no
                # compression); in that case the ratio check is meaningless
                # and the per-entry byte cap below is the real safeguard.
                # We only apply the ratio check once the entry is large
                # enough that the ratio is meaningful — tiny entries
                # naturally show huge ratios that aren't exploitable.
                if (
                    info.compress_size > _MIN_COMPRESS_SIZE_FOR_RATIO
                    and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    skipped_count += 1
                    logger.warning(
                        "container entry rejected: compression ratio {:.1f}x exceeds limit",
                        info.file_size / info.compress_size,
                    )
                    continue

                # Cumulative compressed-byte cap. Even if every individual
                # entry passes the per-entry cap, we will not burn more than
                # this much total bandwidth through zf.open().
                if compressed_consumed + info.compress_size > _MAX_TOTAL_COMPRESSED_BYTES:
                    truncated = True
                    break

                try:
                    # Never expand an entry without a bound: ZIP bombs can
                    # advertise or produce enormous uncompressed payloads.
                    # Read at most ``_MAX_PER_ENTRY_BYTES + 1`` so we can
                    # detect truncation explicitly below.
                    with zf.open(info) as entry:
                        raw = entry.read(_MAX_PER_ENTRY_BYTES + 1)
                except (zipfile.BadZipFile, RuntimeError, OSError):
                    continue

                # Approximate compressed bytes consumed: if compressed_size is
                # 0 (STORED) it equals file_size, so use that as fallback.
                compressed_consumed += info.compress_size or info.file_size

                if len(raw) > _MAX_PER_ENTRY_BYTES:
                    raw = raw[:_MAX_PER_ENTRY_BYTES]
                    truncated = True

                # Best-effort decode: HDX HTML is almost always GBK / GB18030.
                text: Optional[str] = None
                for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "big5", "latin-1"):
                    try:
                        text = raw.decode(enc)
                        if enc in ("utf-8", "utf-8-sig") and text.count("\ufffd") > len(text) * 0.02:
                            text = None
                            continue
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if text is None:
                    continue

                if info.filename.lower().endswith(
                    (".html", ".htm", ".xhtml", ".xml", ".opf", ".ncx")
                ):
                    body = _strip_html(text)
                else:
                    body = text

                body = body.strip()
                if not body:
                    continue

                if len(body) > _MAX_PER_ENTRY_CHARS:
                    body = body[:_MAX_PER_ENTRY_CHARS] + "\n…(本章节截断)"
                    truncated = True

                heading = f"## {info.filename}\n"
                remaining = _MAX_TOTAL_CHARS - total
                section = (heading + body)[:remaining]
                parts.append(section)
                total += len(section)
                entry_count += 1
                if len(section) < len(heading) + len(body):
                    truncated = True
                    break
            skipped_count = max(total_entries - entry_count, 0)
    except (zipfile.BadZipFile, OSError):
        return None

    if not parts:
        return None

    md = "\n\n".join(parts)[:_MAX_TOTAL_CHARS]
    if truncated and len(md) < _MAX_TOTAL_CHARS:
        md += (
            f"\n\n---\n*已截断：共索引 {entry_count} 个文本条目，"
            f"剩余 {skipped_count} 个条目未展开。*"
        )
        md = md[:_MAX_TOTAL_CHARS]
    return ContainerText(
        markdown=md,
        entry_count=entry_count,
        skipped_count=skipped_count,
        truncated=truncated,
    )


def looks_like_zip_container(path: Path) -> bool:
    """Cheap header sniff — does *path* start with PK\\x03\\x04?"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except Exception:
        return False
