"""Role policy regression tests for document mutations."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.deps import require_operator


class DocumentRolePolicyTest(unittest.TestCase):
    def test_viewer_cannot_mutate_documents(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            require_operator(SimpleNamespace(role="viewer"))
        self.assertEqual(raised.exception.status_code, 403)

    def test_operator_can_mutate_documents(self) -> None:
        user = SimpleNamespace(role="operator")
        self.assertIs(require_operator(user), user)

    def test_admin_can_mutate_documents(self) -> None:
        user = SimpleNamespace(role="admin")
        self.assertIs(require_operator(user), user)


if __name__ == "__main__":
    unittest.main()
