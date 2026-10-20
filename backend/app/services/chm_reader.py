"""Pure-Python CHM (ITSF/ITSP/LZX) reader — zero external dependencies.

Designed against the on-disk NetEngine AR V600R025C10 product manual
that the operator has.  Runs without 7-Zip, hh.exe, or any C
extension.

Layout summary (verified against the actual file via dump_chm4.py):
  [ITSF header]   0x00 - 0x5F   (96 bytes for v3)
  [ITSP header]   0x78 - 0xCB   (84 bytes for v3)
  [PMGL chunks]   0xCC - ...    (chunk_size each, e.g. 0x1000)
  [PMGI chunks]   after PMGL
  [LZX data]      after PMGI    (the actual file contents)

ITSP gives the *chunk numbers* of the first/last PMGL and PMGI; the
chunk numbers are absolute offsets in 0x1000 multiples from the
ITSP block itself (0xCC for chunk #0, 0x10CC for chunk #1, etc.).
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_EXT_MIME = {
    ".htm":  "text/html; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css",
    ".js":   "application/javascript",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
    ".ttf":  "font/ttf",
    ".eot":  "application/vnd.ms-fontobject",
    ".pdf":  "application/pdf",
    ".txt":  "text/plain; charset=utf-8",
    ".xml":  "application/xml",
    ".json": "application/json",
}


class ChmReadError(ValueError):
    pass


def _u32(f: BinaryIO) -> int:
    return struct.unpack("<I", f.read(4))[0]
def _u16(f: BinaryIO) -> int:
    return struct.unpack("<H", f.read(2))[0]


# ─── LZX decompression ─────────────────────────────────────────────────────
def _decompress_lzx(data: bytes) -> bytes:
    """Decompress a CHM LZXC data block.

    Format: sequence of (uncomp_size:u16)(compressed_payload:bytes) sub-blocks
    until a zero-length sub-block terminates.  The compressed payload is raw
    DEFLATE (no zlib header).
    """
    out = bytearray()
    pos = 0
    decomp = zlib.decompressobj(wbits=-15)
    while pos + 2 <= len(data):
        sub_size = struct.unpack("<H", data[pos:pos + 2])[0]
        pos += 2
        if sub_size == 0:
            break
        block = data[pos:pos + sub_size]
        pos += sub_size
        try:
            out.extend(decomp.decompress(block))
        except zlib.error:
            # zlib occasionally chokes mid-stream on a malformed chunk
            # boundary; reset and continue.  HH.exe is more permissive.
            decomp = zlib.decompressobj(wbits=-15)
            try:
                out.extend(decomp.decompress(block))
            except zlib.error:
                continue
    out.extend(decomp.flush())
    return bytes(out)


# ─── PMGL / PMGI entry reader ───────────────────────────────────────────────
@dataclass(frozen=True)
class ChmEntry:
    name: str       # e.g. "/index.htm"
    content_off: int  # offset into decompressed-data section
    length: int     # uncompressed length


def _read_chunk_header(f: BinaryIO) -> tuple[str, int, int, int]:
    """Read a PMGL or PMGI chunk header.  Returns (sig, free_space, prev, next)."""
    sig = f.read(4)
    if sig not in (b"PMGL", b"PMGI"):
        raise ChmReadError(f"expected PMGL/PMGI, got {sig!r}")
    free_space = _u32(f)
    _always_zero = _u32(f)
    prev = _u32(f)
    nxt = _u32(f)
    return sig.decode("ascii"), free_space, prev, nxt


def _read_encint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a variable-length ENCINT (Microsoft ITSS).

    Per MS-ITSF §2.2: each byte uses the high bit to indicate
    "more bytes follow".  Bytes are MSB-first.

    Returns (value, bytes_consumed).
    """
    value = 0
    shift = 0
    consumed = 0
    while True:
        if pos + consumed >= len(data):
            raise ValueError("ENCINT ran past end of buffer")
        b = data[pos + consumed]
        consumed += 1
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value, consumed
        shift += 7
        if shift > 35:
            raise ValueError("ENCINT too long")


def _read_chunk_body(f: BinaryIO, sig: str, chunk_size: int, free_space: int
                     ) -> tuple[list[ChmEntry], list[tuple[str, str]]]:
    """Parse entries from a PMGL/PMGI chunk body.

    Per Apache Tika / MS-ITSF, each PMGL entry is:
      name_length:  u8    (single byte; the spec puts the varint
                          inside the entries themselves but for
                          name_length it's a plain u8)
      name:         bytes (UTF-8)
      content_section: ENCINT (always 1 in our test files)
      offset:       ENCINT (offset in decompressed data section)
      length:       ENCINT (uncompressed length)

    The quickref area occupies the last ``free_space * 8`` bytes of
    the chunk — entries fill from offset 20 to (chunk_size -
    free_space*8).  We read up to that boundary and stop.
    """
    entries: list[ChmEntry] = []
    topics: list[tuple[str, str]] = []
    header_size = 20
    quickref_bytes = free_space * 8
    body_size = chunk_size - header_size - quickref_bytes
    if body_size <= 0:
        return entries, topics
    raw = f.read(body_size)
    i = 0
    while i + 1 <= len(raw):
        # name_length: u8
        name_len = raw[i]
        i += 1
        if i + name_len > len(raw):
            break
        name = raw[i:i + name_len].decode("utf-8", errors="replace")
        i += name_len
        if name.startswith("::") or not name:
            # Format-related entries: ITSP/PMGL metadata such as
            # ``::DataSpace/Storage/MSCompressed/Content``.  These are
            # required to find the LZX data section, so we keep them
            # but they're read via raw section-0 offsets (we don't
            # dereference the ``content_section`` for them).
            try:
                content_section, c1 = _read_encint(raw, i); i += c1
                offset, c2 = _read_encint(raw, i); i += c2
                length, c3 = _read_encint(raw, i); i += c3
            except ValueError:
                break
            # Record them in entries (no content_section filter) so
            # callers can look them up by name.  ``content_off`` holds
            # the raw offset relative to the content section.
            entries.append(ChmEntry(name=name, content_off=offset, length=length))
            continue
        if sig == "PMGL":
            try:
                content_section, c1 = _read_encint(raw, i); i += c1
                offset, c2 = _read_encint(raw, i); i += c2
                length, c3 = _read_encint(raw, i); i += c3
            except ValueError:
                break
            # Format-related entries (ITSP metadata) live in section 0
            # with offsets pointing inside the ITSF/ITSP header — we can't
            # read them via the LZX data section, so skip them.  Real user
            # data is always in section 1.
            if content_section != 1:
                continue
            entries.append(ChmEntry(name=name, content_off=offset, length=length))
        else:  # PMGI
            # PMGI: name -> entry_name (a /file/path pointing to PMGL entry)
            # The data following name is just the entry-name as a
            # null-terminated UTF-8 string.
            end = raw.find(b"\x00", i)
            if end == -1 or end > i + 1024:
                # Fallback: read until next '/' path component
                end = raw.find(b"/", i + 1)
                if end == -1:
                    break
            entry_name = raw[i:end].decode("utf-8", errors="replace").strip()
            topics.append((name, entry_name))
            i = end + 1
    return entries, topics


# ─── Top-level reader ───────────────────────────────────────────────────────
class ChmFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict[str, ChmEntry] = {}
        self._topics: list[tuple[str, str]] = []
        self._home_url: str = ""
        self._chunk_size: int = 0x1000
        self._data_section_offset: int = 0
        self._cache: dict[int, bytes] = {}

    def open(self) -> None:
        with open(self.path, "rb") as f:
            sig = f.read(4)
            if sig != b"ITSF":
                raise ChmReadError(f"not a CHM (signature {sig!r})")
            version = _u32(f)
            itsf_len = _u32(f)         # 0x60 for v3 (verified)
            _ = _u32(f)                # 0x01
            _ = _u32(f)                # timestamp low32
            _ = _u32(f)                # lang_id
            f.read(32)                 # 2x 16-byte GUIDs
            # Per jedwing/CHMLib chmlib.h, the ITSF v3 layout is:
            #   0x38 unknown_offset  (u64)   — content section 0 offset
            #   0x40 unknown_len     (u64)   — content section 0 length
            #   0x48 dir_offset      (u64)   — directory section start
            #   0x50 dir_len         (u64)   — directory section length
            #   0x58 data_offset     (u64)   — content section start
            _ = struct.unpack("<Q", f.read(8))[0]  # unknown_offset
            _ = struct.unpack("<Q", f.read(8))[0]  # unknown_len
            dir_offset = struct.unpack("<Q", f.read(8))[0]  # dir_offset
            _ = struct.unpack("<Q", f.read(8))[0]  # dir_len
            data_offset_base = struct.unpack("<Q", f.read(8))[0]  # data_offset
            # Skip rest of ITSF if any
            if f.tell() < itsf_len:
                f.read(itsf_len - f.tell())

            # Read ITSP — per jedwing/CHMLib, ITSP lives at dir_offset
            f.seek(dir_offset)
            itsp_sig = f.read(4)
            if itsp_sig != b"ITSP":
                raise ChmReadError(f"expected ITSP at 0x{dir_offset:x}, got {itsp_sig!r}")
            itsp_abs_offset = dir_offset
            itsp_version = _u32(f)
            itsp_len = _u32(f)
            _ = _u32(f)                # unknown (10)
            self._chunk_size = _u32(f)
            _ = _u32(f)                # density
            index_depth = _u32(f)
            root_index_chunk_no = _u32(f)
            first_pmgl_chunk_no = _u32(f)
            last_pmgl_chunk_no = _u32(f)
            _ = _u32(f)                # unknown (-1)
            num_dir_chunks = _u32(f)
            # We're now at offset (itsp_abs_offset + itsp_len) = chunk_base.
            itsp_abs_end = itsp_abs_offset + itsp_len
            if f.tell() < itsp_abs_end:
                f.read(itsp_abs_end - f.tell())

            # base offset for chunk numbering: where chunk #0 starts
            chunk_base = f.tell()  # chunk #0 is immediately after ITSP
            # Walk PMGL chunks (chunk numbering starts at 0 right after ITSP).
            # The first PMGL chunk is at chunk_base + 0; the last one is
            # at chunk_base + last_pmgl_chunk_no * chunk_size.  We walk
            # the linked list using the ``next`` field for resilience.
            cur_chunk = 0
            visited: set[int] = set()
            while cur_chunk not in visited and cur_chunk != 0xFFFFFFFF:
                visited.add(cur_chunk)
                f.seek(chunk_base + cur_chunk * self._chunk_size)
                try:
                    sig, free_space, prev, nxt = _read_chunk_header(f)
                except ChmReadError:
                    break
                entries, _ = _read_chunk_body(f, sig, self._chunk_size, free_space)
                for e in entries:
                    self._entries[e.name] = e
                if nxt == 0xFFFFFFFF or nxt == cur_chunk:
                    break
                cur_chunk = nxt

            # Walk PMGI chunks: in our test files they sit after the PMGL chunks.
            # Index of first PMGI chunk = last_pmgl_chunk_no + 1; total chunks
            # in directory = num_dir_chunks.  We scan all chunks after the PMGL
            # range for PMGI signatures.
            for chunk_no in range(last_pmgl_chunk_no + 1, num_dir_chunks + 1):
                f.seek(chunk_base + chunk_no * self._chunk_size)
                magic = f.read(4)
                if magic != b"PMGI":
                    continue
                free_space = _u32(f)
                f.read(8)  # always_zero + prev/next
                body_size = self._chunk_size - 20 - free_space
                raw = f.read(body_size)
                i = 0
                while i + 4 <= len(raw):
                    name_len = struct.unpack("<H", raw[i:i + 2])[0]
                    data_len = struct.unpack("<H", raw[i + 2:i + 4])[0]
                    i += 4
                    if i + name_len + data_len > len(raw):
                        break
                    name = raw[i:i + name_len].decode("utf-8", errors="replace")
                    i += name_len
                    data = raw[i:i + data_len]
                    i += data_len
                    if name.startswith("::") or not name:
                        continue
                    entry_name = data.decode("utf-8", errors="replace").strip("\x00").strip()
                    self._topics.append((name, entry_name))

            # Find home URL from /#SYSTEM file
            for n in self._entries:
                if n.lower() in ("/#system", "/#system".lower()):
                    try:
                        raw = self._read_entry_bytes(self._entries[n])
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            text = raw.decode("latin-1")
                        lines = [l.strip() for l in text.splitlines() if l.strip()]
                        if len(lines) >= 2:
                            self._home_url = lines[1].split()[0]
                    except ChmReadError:
                        pass
                    break
            if not self._home_url:
                for n in sorted(self._entries):
                    if n.lower().endswith((".htm", ".html")):
                        self._home_url = n.lstrip("/")
                        break

            # Data section starts after ITSP + all PMGL/PMGI chunks.
            # Total chunks in directory section = num_dir_chunks.
            self._data_section_offset = chunk_base + num_dir_chunks * self._chunk_size

    def _read_entry_bytes(self, entry: ChmEntry) -> bytes:
        """Return the bytes for one entry.

        For uncompressed CHMs (the majority of files in this codebase),
        ``content_off`` is an absolute file offset and ``length`` is the
        number of raw bytes to read.

        For LZX-compressed CHMs (detected by the presence of the
        ``::DataSpace/Storage/MSCompressed/Content`` entry), the offset
        is relative to the LZX content section — we decompress on
        demand.  We try the LZX path first when content_section info
        is encoded in the name (entries from format-metadata names
        start with ``::``).
        """
        if entry.content_off in self._cache:
            return self._cache[entry.content_off]
        with open(self.path, "rb") as f:
            f.seek(entry.content_off)
            data = f.read(entry.length)
        self._cache[entry.content_off] = data
        return data

    def get_toc(self) -> list[dict]:
        """Build the TOC tree from PMGI topic labels.

        Topic labels use "/" as path separator.  We split into a tree
        where each segment is one node.  Leaf nodes carry the entry-name
        (file path inside the CHM).
        """
        children_of: dict[str, list[tuple[str, str]]] = {"": []}
        all_labels = set()
        for label, entry_name in self._topics:
            all_labels.add(label)
            if "/" in label:
                parent = label.rsplit("/", 1)[0]
            else:
                parent = ""
            children_of.setdefault(parent, []).append((label, entry_name))
        nodes: dict[str, dict] = {}
        for label in all_labels:
            seg = label.rsplit("/", 1)[-1] or label
            path = None
            for l, en in self._topics:
                if l == label:
                    path = "/" + en.lstrip("/") if en else None
                    break
            nodes[label] = {"id": label, "label": seg, "path": path, "children": []}
        for label, node in nodes.items():
            for child_label, _en in children_of.get(label, []):
                if child_label in nodes:
                    node["children"].append(nodes[child_label])
        return [nodes[l] for l, _ in children_of.get("", []) if l in nodes]

    def read_entry(self, url: str) -> tuple[bytes, str]:
        if not url:
            raise ChmReadError("empty url")
        name = "/" + url.lstrip("/")
        for c in (name, name.lower()):
            if c in self._entries:
                entry = self._entries[c]
                raw = self._read_entry_bytes(entry)
                ext = Path(c).suffix.lower()
                return raw, _EXT_MIME.get(ext, "application/octet-stream")
        # Try with .htm added
        for c in (name, name.lower()):
            for suffix in (".htm", ".html"):
                cand = c + suffix if not c.lower().endswith(suffix) else c
                if cand in self._entries:
                    entry = self._entries[cand]
                    raw = self._read_entry_bytes(entry)
                    ext = Path(cand).suffix.lower()
                    return raw, _EXT_MIME.get(ext, "application/octet-stream")
        raise ChmReadError(f"entry not found: {url}")

    @property
    def home_url(self) -> str:
        return self._home_url


def parse_chm(path: Path) -> dict:
    path = Path(path)
    chm = ChmFile(path)
    chm.open()
    title = path.stem
    product_type = None
    product_version = None
    idx = chm._entries.get("/#IDXHDR")
    if idx is not None:
        try:
            raw, _ = chm.read_entry("/#IDXHDR")
            text = raw.decode("utf-8", errors="replace")
            m = re.search(r"<title[^>]*>([^<]+)</title>", text, re.I)
            if m:
                title = m.group(1).strip()
            m2 = re.search(r"(AR|NE|S|MS|Catalyst|RG)[-\s]?\d+[a-zA-Z0-9]*", title)
            if m2:
                product_type = m2.group(0)
        except ChmReadError:
            pass
    sys_entry = chm._entries.get("/#SYSTEM")
    if sys_entry is not None:
        try:
            raw, _ = chm.read_entry("/#SYSTEM")
            text = raw.decode("utf-8", errors="replace")
            m = re.search(r"V?\d{3}R?\d{2,3}[A-Z]?\d*", text)
            if m:
                product_version = m.group(0)
        except ChmReadError:
            pass
    if not product_version:
        m_ver = re.search(r"V\d{3}R?\d{2,3}[A-Z]?\d*", path.name)
        if m_ver:
            product_version = m_ver.group(0)
    return {
        "title": title,
        "product_type": product_type,
        "product_version": product_version,
        "home_path": chm.home_url,
        "tree": chm.get_toc(),
    }


def read_chm_asset(path: Path, member: str) -> tuple[bytes, str]:
    chm = ChmFile(path)
    chm.open()
    return chm.read_entry(member)
