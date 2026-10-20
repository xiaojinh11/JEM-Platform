"""Server-side validation for document uploads."""
from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.api.v1.documents import validate_document_filename


class DocumentFilenameValidationTest(unittest.TestCase):
    def test_known_manual_extension_is_allowed(self) -> None:
        validate_document_filename("guide.hdx")
        validate_document_filename("archive.tar.gz")

    def test_executable_extension_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            validate_document_filename("payload.exe")
        self.assertEqual(raised.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
