"""Health checks for vendor offline manual packages.

Walks every HTML/CSS member inside a package, extracts any external
references (``<a href>``, ``<link href>``, ``<script src>``, ``<img src>``)
and validates each URL against the actual zip members.

Goal: surface vendor package defects to the operator so the
``看起来没数据 / 一片空白`` experience is never again mistaken for a
frontend bug.  The check is intentionally read-only and tolerant: any
``<a>`` to a missing target, missing CSS link, or missing JS / image is
captured but the scan itself never fails because of an individual file.
"""
from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Iterable
from urllib.parse import unquote, urlsplit

from app.services.offline_manual import _safe_member_path, OfflineManualError

# Vendor HTMLs occasionally use a brittle ``href="<bare>`` or omit quotes;
# we still want to capture those so the report is honest about the source.
_RE_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_RE_SRC = re.compile(r"""src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_RE_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^\)\s]+))""", re.I)


def _attr_groups(match: re.Match[str]) -> list[str]:
    return [g for g in match.groups() if g is not None]


class _ReferenceScanner(HTMLParser):
    """Pull every href / src out of a single HTML or CSS blob."""

    def __init__(self, base: PurePosixPath | None) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base
        self.hrefs: list[str] = []
        self.srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value for name, value in attrs if value is not None}
        if "href" in attr_map:
            self.hrefs.append(attr_map["href"])
        if tag.lower() in {"img", "script", "iframe", "frame", "source", "embed"} and "src" in attr_map:
            self.srcs.append(attr_map["src"])

    def handle_link(self, raw: str) -> None:
        for m in _RE_HREF.finditer(raw):
            self.hrefs.extend(_attr_groups(m))


@dataclass
class OfflineHealth:
    """Aggregated health snapshot for one vendor package."""

    package_path: str
    total_members: int = 0
    html_members: int = 0
    css_members: int = 0
    references_total: int = 0
    references_missing: int = 0
    references_case_only: int = 0
    missing_targets: Counter[str] = field(default_factory=Counter)
    case_only_targets: Counter[str] = field(default_factory=Counter)
    references_by_kind: Counter[str] = field(default_factory=Counter)
    missing_by_kind: Counter[str] = field(default_factory=Counter)
    top_missing_sources: Counter[str] = field(default_factory=Counter)

    @property
    def score(self) -> int:
        """Coarse health score 0-100 — pure 100 means every reference resolves.

        Both truly-missing references and case-folded hits count as
        defects; case-folded hits weight less because the on-disk
        resource actually exists and most HTTP stacks serve it
        correctly (despite the IETF violation).
        """
        if self.references_total == 0:
            return 100
        weighted = self.references_missing + self.references_case_only
        return max(0, min(100, round(self.references_total - weighted) * 100 // self.references_total))

    @property
    def grade(self) -> str:
        if self.references_missing == 0 and self.references_case_only == 0:
            return "ok"
        ratio = (self.references_missing + self.references_case_only) / max(1, self.references_total)
        if ratio <= 0.01:
            return "minor"
        if ratio <= 0.05:
            return "warn"
        return "broken"

    def to_dict(self) -> dict:
        return {
            "package_path": self.package_path,
            "score": self.score,
            "grade": self.grade,
            "total_members": self.total_members,
            "html_members": self.html_members,
            "css_members": self.css_members,
            "references_total": self.references_total,
            "references_missing": self.references_missing,
            "references_case_only": self.references_case_only,
            "references_by_kind": dict(self.references_by_kind),
            "missing_by_kind": dict(self.missing_by_kind),
            "missing_targets": self.missing_targets.most_common(20),
            "case_only_targets": self.case_only_targets.most_common(20),
            "top_missing_sources": self.top_missing_sources.most_common(20),
        }


def _is_external(url: str) -> bool:
    scheme = urlsplit(url).scheme.lower()
    return scheme in {"http", "https", "mailto", "tel", "javascript", "data", "ftp", "ftps"}


def _is_anchor(url: str) -> bool:
    return url.startswith("#") or url.startswith("?")


def _looks_like_vendor_pseudo_query(url: str) -> bool:
    """Skip vendor-internal click handlers that masquerade as relative URLs.

    Huawei / H3C offline manuals embed links whose ``href`` is a bare
    pseudo-query string (e.g. ``cmdqueryname=system-view`` or
    ``cmdqueryname=ftp+172.16.104.110``) — these are consumed by the
    vendor's own JavaScript and never resolve to a zip member.
    Identifying them as "missing resources" floods the report and
    obscures the genuinely broken references.
    """
    body = url.lstrip()
    if not body or body.startswith(("/", "#")):
        return False
    # external schemes are filtered earlier
    if "://" in body:
        return False
    body_lc = body.lower()
    if body_lc.endswith((
        ".html", ".htm", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".json", ".xml", ".woff", ".woff2", ".ttf", ".eot",
    )):
        return False
    head = body.split("?", 1)[0]
    # a real relative path on disk would contain a slash OR a plain
    # ``.ext`` token (with no other punctuation interfering)
    if "/" in head:
        return False
    if "." in head:
        # allow vendor ``cmdqueryname=foo+bar`` style which use ``+``
        # for spaces and ``=`` for key/value pairs
        if "=" in head or "+" in head:
            return True
        return False
    return True


def _candidate_targets(base: PurePosixPath | None, ref: str) -> list[str]:
    """Return possible resolved zip members for a single reference."""
    ref = ref.strip()
    if not ref or _is_external(ref) or _is_anchor(ref):
        return []
    if _looks_like_vendor_pseudo_query(ref):
        return []
    ref_path, _, _ = ref.partition("#")
    ref_path, _, _ = ref_path.partition("?")
    if not ref_path:
        return []
    try:
        decoded = unquote(ref_path)
    except UnicodeDecodeError:
        return []
    decoded = decoded.replace("\\", "/")
    if decoded.startswith("/"):
        try:
            decoded = _safe_member_path(decoded.lstrip("/"))
        except OfflineManualError:
            return []
    try:
        decoded = _safe_member_path(decoded)
    except OfflineManualError:
        return []
    if base is None:
        resolved = [decoded]
    else:
        target = (base / decoded).as_posix()
        try:
            target = _safe_member_path(target)
        except OfflineManualError:
            return []
        resolved = [target, decoded]
    # dedupe while preserving order
    seen: set[str] = set()
    return [r for r in resolved if not (r in seen or seen.add(r))]


def _scan_html(member: str, blob: bytes) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:
        return [], []
    base = PurePosixPath(member).parent
    hrefs: list[tuple[str, str]] = []
    srcs: list[tuple[str, str]] = []
    parser = _ReferenceScanner(base)
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Bad HTML — fall back to regex sweep
        parser.hrefs = []
        parser.srcs = []
    for href in parser.hrefs:
        hrefs.append((member, href))
    for src in parser.srcs:
        srcs.append((member, src))
    # regex fallback for the broken-HTML path
    if not hrefs and not srcs:
        for m in _RE_HREF.finditer(text):
            hrefs.extend([(member, g) for g in _attr_groups(m)])
        for m in _RE_SRC.finditer(text):
            srcs.extend([(member, g) for g in _attr_groups(m)])
    return hrefs, srcs


def _scan_css(member: str, blob: bytes) -> list[tuple[str, str]]:
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:
        return []
    base = PurePosixPath(member).parent
    refs: list[tuple[str, str]] = []
    for m in _RE_URL.finditer(text):
        for ref in _attr_groups(m):
            refs.append((member, ref))
    return refs


def inspect_offline_health(
    path,
    *,
    max_scan_bytes: int = 4 * 1024 * 1024,
    max_references: int = 200_000,
) -> OfflineHealth:
    """Open the package once and report on every cross-reference it makes."""
    health = OfflineHealth(package_path=str(path))
    path_str = str(path)
    try:
        archive = zipfile.ZipFile(path_str)
    except (zipfile.BadZipFile, FileNotFoundError, OSError):
        return health
    try:
        members = archive.namelist()
        health.total_members = len(members)

        # Build a case-insensitive index so that vendor packages that mix
        # ``mediaQuery.css`` (in the HTML) with ``mediaquery.css`` (on
        # disk) still resolve. ``exact_index`` is used to flag genuinely
        # missing references that *only* survive via case-folding — those
        # are still counted as missing because the IETF-correct URL
        # didn't match the on-disk name byte-for-byte.
        exact_index: set[str] = set(members)
        lower_index: dict[str, str] = {name.lower(): name for name in members}

        html_members: list[str] = []
        css_members: list[str] = []
        for name in members:
            lower = name.lower()
            if lower.endswith((".html", ".htm")) and not name.endswith("/"):
                html_members.append(name)
            elif lower.endswith(".css") and not name.endswith("/"):
                css_members.append(name)

        health.html_members = len(html_members)
        health.css_members = len(css_members)

        def _exists(target: str) -> bool:
            if target in exact_index:
                return True
            return target.lower() in lower_index

        def _record(refs: Iterable[tuple[str, str]], kind: str) -> None:
            for source, ref in refs:
                if health.references_total >= max_references:
                    return
                targets = _candidate_targets(PurePosixPath(source).parent, ref)
                if not targets:
                    continue
                health.references_total += 1
                health.references_by_kind[kind] += 1
                # walk every candidate and pick the first hit, preferring
                # an exact match over a case-folded one
                chosen_match: str | None = None
                chosen_case_only: str | None = None
                for target in targets:
                    if target in exact_index:
                        chosen_match = target
                        break
                    if target.lower() in lower_index:
                        chosen_case_only = chosen_case_only or target
                if chosen_match is not None:
                    continue
                if chosen_case_only is not None:
                    # on-disk file exists but the HTML reference is the
                    # wrong case — vendor package bug, not a missing file
                    health.references_case_only += 1
                    health.case_only_targets[chosen_case_only] += 1
                    continue
                health.references_missing += 1
                health.missing_by_kind[kind] += 1
                health.missing_targets[targets[0]] += 1
                health.top_missing_sources[source] += 1

        for member in html_members:
            try:
                info = archive.getinfo(member)
            except KeyError:
                continue
            if info.file_size > max_scan_bytes:
                continue
            try:
                blob = archive.read(info)
            except (RuntimeError, OSError):
                continue
            hrefs, srcs = _scan_html(member, blob)
            _record(hrefs, "html_href")
            _record(srcs, "html_src")

        for member in css_members:
            try:
                info = archive.getinfo(member)
            except KeyError:
                continue
            if info.file_size > max_scan_bytes:
                continue
            try:
                blob = archive.read(info)
            except (RuntimeError, OSError):
                continue
            _record(_scan_css(member, blob), "css_url")

    finally:
        try:
            archive.close()
        except Exception:
            pass
    return health
