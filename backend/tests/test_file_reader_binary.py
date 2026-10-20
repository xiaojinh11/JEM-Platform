"""Binary detection for legacy Office formats that look like text."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.utils.file_reader import smart_read


class BinaryExtensionDetectionTest(unittest.TestCase):
    def _write(self, suffix: str, head: bytes) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(head)
        tmp.close()
        return Path(tmp.name)

    def test_legacy_word_doc_is_flagged_binary(self) -> None:
        """A real OLE2 header lacks NUL bytes in the first 512 bytes, so the
        ``_looks_like_text`` heuristic alone would mis-decode it as text.
        The extension-based override must kick in first.
        """
        path = self._write(
            ".doc",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2 signature
            + b"Word Document".ljust(512, b"\x00"),
        )
        try:
            result = smart_read(path, max_chars=200)
            self.assertTrue(result.is_binary)
            self.assertEqual(result.content, "")
        finally:
            path.unlink(missing_ok=True)

    def test_pdf_is_binary(self) -> None:
        path = self._write(".pdf", b"%PDF-1.4\n%hello\n")
        try:
            result = smart_read(path)
            self.assertTrue(result.is_binary)
        finally:
            path.unlink(missing_ok=True)

    def test_plain_markdown_still_reads_as_text(self) -> None:
        path = self._write(".md", b"# Title\n\n- item 1\n- item 2\n")
        try:
            result = smart_read(path)
            self.assertFalse(result.is_binary)
            self.assertIn("Title", result.content)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
