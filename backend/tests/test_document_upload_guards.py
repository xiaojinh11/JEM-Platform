"""Regression tests for the document upload/read safety boundary."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.utils.container_reader import extract_container
from app.utils.file_reader import smart_read
from app.utils.helpers import safe_join


class DocumentUploadGuardsTest(unittest.TestCase):
    def test_safe_join_rejects_sibling_directory_prefix(self) -> None:
        root = Path(tempfile.mkdtemp()) / "storage"
        with self.assertRaises(ValueError):
            safe_join(root, "..", "storage-escape", "proof.txt")

    def test_text_reader_does_not_depend_on_full_read_bytes(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            path = Path(tmp.name)
            tmp.write(("网络配置文档\n" * 100_000).encode("utf-8"))
        try:
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                result = smart_read(path, max_chars=128)
            self.assertFalse(result.is_binary)
            self.assertLessEqual(len(result.content), 128)
        finally:
            path.unlink(missing_ok=True)

    def test_container_reader_does_not_expand_entry_beyond_declared_cap(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".hdx", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manual/topic.html", "A" * 500_000)
            result = extract_container(path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertLessEqual(len(result.markdown), 100_000)
            self.assertTrue(result.truncated)
        finally:
            path.unlink(missing_ok=True)

    def test_container_reader_honors_total_character_cap(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".hdx", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manual/topic.html", "B" * 100_000)
            with patch("app.utils.container_reader._MAX_TOTAL_CHARS", 10):
                result = extract_container(path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertLessEqual(len(result.markdown), 10)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
