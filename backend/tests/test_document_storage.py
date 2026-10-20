"""Regression tests for document storage ownership and managed paths."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.documents import import_file


class _FakeQuery:
    def order_by(self, *_args):
        return self

    def all(self):
        return []


class _FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.docs = []
        self._next_id = 1
        self.fail_commit = fail_commit
        self.rolled_back = False

    def add(self, doc) -> None:
        if doc not in self.docs:
            doc.id = self._next_id
            self._next_id += 1
            self.docs.append(doc)

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit failed")
        return None

    def rollback(self) -> None:
        self.rolled_back = True

    def refresh(self, _doc) -> None:
        return None

    def flush(self) -> None:
        return None

    def query(self, *_args):
        return _FakeQuery()


class DocumentStorageTest(unittest.TestCase):
    def test_repeated_uploads_with_same_filename_get_distinct_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.md"
            source.write_text("# handbook\n", encoding="utf-8")
            storage = root / "documents"
            db = _FakeSession()
            with (
                patch("app.services.documents.settings.storage_documents_dir", storage),
                patch("app.services.documents.index_document"),
                patch("app.services.documents.obsidian_service.mirror_document"),
            ):
                first = import_file(
                    db=db,
                    uploaded_by="tester",
                    title="One",
                    vendor="vendor",
                    category="产品手册",
                    version_tag=None,
                    source=None,
                    file_path=source,
                    original_filename="same.md",
                )
                second = import_file(
                    db=db,
                    uploaded_by="tester",
                    title="Two",
                    vendor="vendor",
                    category="产品手册",
                    version_tag=None,
                    source=None,
                    file_path=source,
                    original_filename="same.md",
                )

            self.assertEqual(first.filename, "same.md")
            self.assertEqual(second.filename, "same.md")
            self.assertNotEqual(first.storage_path, second.storage_path)
            self.assertFalse(Path(first.storage_path).is_absolute())
            self.assertTrue((storage / first.storage_path).exists())
            self.assertTrue((storage / second.storage_path).exists())

    def test_storage_path_embeds_hash_and_doc_id(self) -> None:
        """One-row-one-file invariant: deleting one row never touches another row's file.

        Pre-fix, storage_path was keyed only on a uuid + filename, so two
        records with identical bytes (or filenames) could share a path. Now
        every row's file path includes ``{content_hash}_{doc_id}_``, which
        guarantees the on-disk payload is owned by exactly one row.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.md"
            source.write_text("# handbook\n", encoding="utf-8")
            storage = root / "documents"
            db = _FakeSession()
            with (
                patch("app.services.documents.settings.storage_documents_dir", storage),
                patch("app.services.documents.index_document"),
                patch("app.services.documents.obsidian_service.mirror_document"),
            ):
                first = import_file(
                    db=db,
                    uploaded_by="tester",
                    title="One",
                    vendor="vendor",
                    category="产品手册",
                    version_tag=None,
                    source=None,
                    file_path=source,
                    original_filename="same.md",
                )
                second = import_file(
                    db=db,
                    uploaded_by="tester",
                    title="Two",
                    vendor="vendor",
                    category="产品手册",
                    version_tag=None,
                    source=None,
                    file_path=source,
                    original_filename="same.md",
                )

            # Both files live under the same content-hash bucket (since the
            # content is identical), but each row's storage_path includes its
            # own doc_id, so the files are physically distinct.
            self.assertNotEqual(first.id, second.id)
            self.assertTrue(first.storage_path.endswith(f"_{first.id}_same.md"))
            self.assertTrue(second.storage_path.endswith(f"_{second.id}_same.md"))
            # And both files actually exist on disk.
            self.assertTrue((storage / first.storage_path).exists())
            self.assertTrue((storage / second.storage_path).exists())

    def test_failed_commit_removes_copied_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.md"
            source.write_text("# handbook\n", encoding="utf-8")
            storage = root / "documents"
            db = _FakeSession(fail_commit=True)
            with (
                patch("app.services.documents.settings.storage_documents_dir", storage),
                patch("app.services.documents.index_document"),
                patch("app.services.documents.obsidian_service.mirror_document"),
            ):
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    import_file(
                        db=db,
                        uploaded_by="tester",
                        title="One",
                        vendor="vendor",
                        category="产品手册",
                        version_tag=None,
                        source=None,
                        file_path=source,
                        original_filename="same.md",
                    )

            self.assertEqual(list(storage.rglob("*.*")), [])


if __name__ == "__main__":
    unittest.main()
