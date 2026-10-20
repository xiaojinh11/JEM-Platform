"""Helper utilities."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Maximum rows allowed in CSV imports to prevent DoS
MAX_CSV_ROWS = 10000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_dir(p: Path | str) -> Path:
    path = Path(p)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    """Strip path separators and dangerous characters."""
    name = name.replace("\\", "_").replace("/", "_").strip()
    return "".join(ch if ch.isalnum() or ch in (" ", ".", "-", "_", "(", ")", "（", "）", "中", "文") else "_" for ch in name)


def parse_csv_devices(content: bytes) -> list[dict[str, Any]]:
    """Parse a UTF-8 (or GBK) CSV with a documented header."""
    raw = content.decode("utf-8", errors="ignore").strip()
    if not raw:
        raise ValueError("CSV is empty")
    # BOM guard
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise ValueError("CSV missing header")
    expected = {"name", "vendor"}
    missing = expected - {c.strip() for c in reader.fieldnames}
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(reader):
        # Enforce row limit to prevent DoS via oversized CSV
        if idx >= MAX_CSV_ROWS:
            raise ValueError(f"CSV exceeds {MAX_CSV_ROWS} rows")
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if not row.get("name"):
            continue
        rows.append(row)
    return rows


def chunked(items: list, size: int) -> Iterable[list]:
    if size <= 0:
        size = 1
    for i in range(0, len(items), size):
        yield items[i : i + size]


def safe_join(root: Path, *parts: str) -> Path:
    """Join paths and ensure the result is inside the root (anti-traversal).

    Defends against:
    - Relative traversal: ../../../etc/passwd
    - Windows drive switch: D:\sensitive
    - UNC paths: \\\\server\\share
    - Mixed separators: ..\\..\\Windows\\System32
    """
    root_resolved = root.resolve()

    # Reject UNC paths and absolute paths in parts
    for part in parts:
        if part == "":
            raise ValueError("empty path component forbidden")
        if part.startswith("\\\\") or (len(part) >= 2 and part[1] == ":"):
            raise ValueError("absolute or UNC paths forbidden")

    # Normalise separators before resolving. ``Path.resolve`` already collapses
    # ``..`` segments on POSIX, but explicit normalisation gives us a stable
    # canonical form to compare against.
    normalised = [str(part).replace("\\", "/") for part in parts]
    target = root_resolved.joinpath(*normalised).resolve()

    # Use relative_to for the final check. It raises ValueError if the target
    # is not strictly inside the root, including sibling-prefix attacks that
    # string-based prefix checks miss on case-sensitive filesystems.
    try:
        rel = target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("path traversal detected") from exc

    # Defence in depth: refuse a relative path that still steps outside even
    # if relative_to somehow succeeded (e.g. symlink racing).
    if rel.parts == ("..",) or any(p == ".." for p in rel.parts):
        raise ValueError("path traversal detected")

    return target


def managed_path(root: Path, stored_path: Path | str) -> Path:
    """Resolve a stored path and require it to remain under *root*."""
    root_resolved = root.resolve()
    candidate = Path(stored_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("stored path is outside the managed directory") from exc
    return candidate
