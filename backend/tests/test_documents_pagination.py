"""Regression tests for paginated document list + vendor/category filters."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import patch

from app.services.documents import list_documents


class _FakeRow(SimpleNamespace):
    """Minimal Document stand-in: has the fields the service touches."""


class _EqClause(SimpleNamespace):
    """Mimics SQLAlchemy ``Column.__eq__`` for tests."""


def _make_eq(column_key: str, value):
    clause = SimpleNamespace()
    clause.left = SimpleNamespace(key=column_key)
    clause.right = SimpleNamespace(value=value)
    return clause


class _FakeQuery:
    def __init__(self, rows: List[_FakeRow]) -> None:
        self._rows = rows
        self._filters = []
        self._offset = 0
        self._limit: Optional[int] = None

    def filter(self, clause):
        self._filters.append(clause)
        return self

    def with_entities(self, _col):
        return self

    def order_by(self, *_args):
        return self

    def offset(self, n: int):
        self._offset = n
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def count(self) -> int:
        return sum(1 for r in self._rows if self._matches(r))

    def all(self) -> List[_FakeRow]:
        matched = [r for r in self._rows if self._matches(r)]
        sliced = matched[self._offset :]
        if self._limit is not None:
            sliced = sliced[: self._limit]
        return sliced

    def _matches(self, r: _FakeRow) -> bool:
        for clause in self._filters:
            key = getattr(clause.left, "key", None)
            value = getattr(clause.right, "value", None)
            if key == "vendor" and r.vendor != value:
                return False
            if key == "category" and r.category != value:
                return False
        return True


class _FakeSession:
    def __init__(self, rows: List[_FakeRow]) -> None:
        self.rows = rows

    def query(self, _model):
        return _FakeQuery(self.rows)


class _Col:
    def __init__(self, key: str) -> None:
        self.key = key

    def __eq__(self, other):
        return _make_eq(self.key, other)

    def desc(self):
        # The service uses ``Document.created_at.desc()`` — return a
        # self-equality marker that the fake query's ``order_by`` ignores.
        return ("desc", self.key)


class _DocStub:
    vendor = _Col("vendor")
    category = _Col("category")
    id = _Col("id")
    created_at = _Col("created_at")


class DocumentPaginationTest(unittest.TestCase):
    def setUp(self) -> None:
        # ``list_documents`` references the symbol ``Document`` from its own
        # module. Patch that name with a stub that produces equality clauses
        # the fake query understands.
        self._patcher = patch("app.services.documents.Document", new=_DocStub)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _make_rows(self, n: int) -> List[_FakeRow]:
        return [
            _FakeRow(
                id=i,
                title=f"t{i}",
                vendor="huawei" if i % 2 == 0 else "cisco",
                category="产品手册",
                filename=f"f{i}.md",
                size_bytes=10,
                content_hash="x" * 64,
                summary=None,
                source=None,
                uploaded_by="u",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            )
            for i in range(n)
        ]

    def test_default_pagination_returns_total_and_slice(self) -> None:
        db = _FakeSession(self._make_rows(120))

        result = list_documents(db, limit=50, offset=0)

        self.assertEqual(result["total"], 120)
        self.assertEqual(len(result["items"]), 50)
        self.assertEqual(result["limit"], 50)
        self.assertEqual(result["offset"], 0)

    def test_vendor_filter_narrows_total(self) -> None:
        rows = [
            _FakeRow(
                id=i,
                title=f"t{i}",
                vendor="huawei" if i % 3 != 0 else "cisco",
                category="产品手册",
                filename=f"f{i}.md",
                size_bytes=10,
                content_hash="x" * 64,
                summary=None,
                source=None,
                uploaded_by="u",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            )
            for i in range(30)
        ]
        db = _FakeSession(rows)

        result = list_documents(db, vendor="cisco", limit=20, offset=0)

        self.assertEqual(result["total"], 10)
        for r in result["items"]:
            self.assertEqual(r.vendor, "cisco")


if __name__ == "__main__":
    unittest.main()
