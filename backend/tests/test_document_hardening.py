"""Tests for the new defensive layers added in C3/C5/C9:
zip-bomb ratio rejection, storage_path sanitisation, async parse
dispatch isolation."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.utils.container_reader import (
    _MAX_COMPRESSION_RATIO,
    _MIN_COMPRESS_SIZE_FOR_RATIO,
    extract_container,
)


class ZipBombDefenceTest(unittest.TestCase):
    def test_high_ratio_entry_is_rejected(self) -> None:
        """A 4 GB STORED entry compressed to ~4 KB must be skipped.

        We can't realistically produce 4 GB on disk in CI, so we use a
        proportionally small but high-ratio entry that crosses the
        threshold. The pre-fix code would have inflated the entry into
        memory before truncating; the new ratio check skips it outright.
        """
        with tempfile.NamedTemporaryFile(suffix=".hdx", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                # STORED entry; ``compress_size`` equals ``file_size`` so
                # the ratio check is bypassed (which is intentional —
                # STORED cannot be a bomb vector). Now use DEFLATED.
                archive.close()
            # Re-open and write a deliberately high-ratio DEFLATED entry.
            # We bypass the ratio-check's per-entry exemption by lowering
            # the test threshold locally: at default settings, ``A`` *
            # millions of bytes still compresses below 4 KB which the
            # reader rightly exempts from ratio checks (a 4 KB entry can
            # never OOM us). To exercise the ratio guard we patch the
            # threshold for this test only.
            with patch(
                "app.utils.container_reader._MIN_COMPRESS_SIZE_FOR_RATIO", 64
            ):
                with zipfile.ZipFile(
                    path, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr(
                        "manual/bomb.html", "A" * (_MAX_COMPRESSION_RATIO * 1024)
                    )
                info = zipfile.ZipFile(path).infolist()[0]
                result = extract_container(path)
            self.assertGreater(info.compress_size, 64)
            self.assertGreater(
                info.file_size / info.compress_size,
                _MAX_COMPRESSION_RATIO,
            )

            # The bomb entry should have been skipped; container reader
            # therefore returns None (no surviving text entries).
            self.assertIsNone(result)
        finally:
            path.unlink(missing_ok=True)

    def test_normal_compressed_container_still_works(self) -> None:
        """Sanity check: legitimate text content under the cap is preserved."""
        with tempfile.NamedTemporaryFile(suffix=".hdx", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manual/page.html", "<h1>配置</h1><p>display version</p>")
            result = extract_container(path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertIn("配置", result.markdown)
            self.assertIn("display version", result.markdown)
            self.assertFalse(result.truncated)
        finally:
            path.unlink(missing_ok=True)


class StoragePathSafetyTest(unittest.TestCase):
    def test_document_storage_path_rejects_traversal_segments(self) -> None:
        """A row whose storage_path contains ``..`` must not be resolved.

        Pre-fix the resolver silently let ``..`` segments through after
        ``Path.resolve()`` normalisation, which meant a row-level SQL
        tamper could read arbitrary files on disk. The new resolver
        rejects ``..`` before any I/O.
        """
        from app.services.documents import document_storage_path

        poisoned = SimpleNamespace(
            id=42,
            storage_path="ab/../../etc/passwd",
        )
        with self.assertRaises(ValueError):
            document_storage_path(poisoned)

    def test_document_storage_path_rejects_pending_placeholder(self) -> None:
        """Mid-upload rows must not be read as if they were complete."""
        from app.services.documents import document_storage_path

        pending = SimpleNamespace(id=1, storage_path="pending")
        with self.assertRaises(ValueError):
            document_storage_path(pending)

    def test_document_storage_path_accepts_normal_layout(self) -> None:
        """Happy path: real storage layout resolves under the documents dir."""
        from app.services.documents import document_storage_path

        ok = SimpleNamespace(
            id=99,
            storage_path="ab/abcd1234abcd1234_99_handbook.md",
        )
        # Patch settings to use a temp root so we don't touch real storage.
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir) / "documents"
            storage_root.mkdir()
            (storage_root / "ab").mkdir()
            target = storage_root / "ab" / "abcd1234abcd1234_99_handbook.md"
            target.write_bytes(b"# hi")
            with patch("app.services.documents.settings.storage_documents_dir", storage_root):
                resolved = document_storage_path(ok)
        self.assertEqual(resolved, target)


class ImportFileIdBindingTest(unittest.TestCase):
    """Verify the new layout: storage_path embeds both hash and doc_id."""

    def test_storage_path_uses_id_segment(self) -> None:
        """After import, the file lives at ``{hash[:2]}/{hash}_{id}_fn``."""
        from app.services.documents import import_file

        class _FakeQ:
            def filter(self, *_a):
                return self

            def order_by(self, *_a):
                return self

            def offset(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def all(self):
                return []

        class _FakeSession:
            def __init__(self):
                self.docs = []
                self._next = 1

            def add(self, doc):
                doc.id = self._next
                self._next += 1
                self.docs.append(doc)

            def flush(self):
                pass

            def commit(self):
                pass

            def refresh(self, _doc):
                pass

            def rollback(self):
                pass

            def query(self, *_a):
                return _FakeQ()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src.md"
            source.write_text("# hi\n", encoding="utf-8")
            storage = root / "documents"
            db = _FakeSession()
            with (
                patch("app.services.documents.settings.storage_documents_dir", storage),
                patch("app.services.documents.index_document"),
                patch("app.services.documents.obsidian_service.mirror_document"),
                patch("app.services.documents.parse_document", create=True),
            ):
                doc = import_file(
                    db=db,
                    uploaded_by="u",
                    title="T",
                    vendor="v",
                    category="c",
                    version_tag=None,
                    source=None,
                    file_path=source,
                    original_filename="src.md",
                )
            # storage_path now includes the row id segment.
            self.assertRegex(doc.storage_path, r"_1_src\.md$")
            self.assertTrue((storage / doc.storage_path).exists())


if __name__ == "__main__":
    unittest.main()
