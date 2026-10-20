"""Scoped session tests for the native offline-manual viewer."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.v1.offline_manual import validate_offline_session
from app.core.security import create_access_token


class _DB:
    def __init__(self, user) -> None:
        self.user = user

    def get(self, _model, _user_id):
        return self.user


class OfflineManualSessionTest(unittest.TestCase):
    def test_session_is_scoped_to_one_document(self) -> None:
        token = create_access_token(
            subject=7,
            extra={"purpose": "offline_manual", "doc_id": 42},
            expires_minutes=10,
        )
        user = SimpleNamespace(id=7, is_active=True)
        self.assertIs(validate_offline_session(token, 42, _DB(user)), user)
        with self.assertRaises(HTTPException):
            validate_offline_session(token, 43, _DB(user))

    def test_disabled_user_is_rejected(self) -> None:
        token = create_access_token(
            subject=7,
            extra={"purpose": "offline_manual", "doc_id": 42},
            expires_minutes=10,
        )
        with self.assertRaises(HTTPException):
            validate_offline_session(token, 42, _DB(SimpleNamespace(id=7, is_active=False)))


if __name__ == "__main__":
    unittest.main()
