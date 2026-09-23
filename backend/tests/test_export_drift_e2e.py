"""E2E coverage for ``GET /api/v1/export/drift`` after the model rename.

The drift export endpoint historically referenced ``current_device_id`` and
``summary`` — fields that don't exist on ``ConfigDrift``. The fix lives
in ``app.api.v1.export._drift``. This test guards against regressions so
a future column rename doesn't crash the JSON endpoint with 500.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_app():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-export-drift-only"
    os.environ["NAW_BOOTSTRAP_ADMIN_USERNAME"] = "admin"
    os.environ["NAW_BOOTSTRAP_ADMIN_PASSWORD"] = "adminpw"

    from app.core.config import settings  # noqa: WPS433
    from app.db.session import Base, get_db  # noqa: WPS433

    settings.database_url = db_url
    engine = create_engine(db_url, future=True)

    from app.models import (  # noqa: WPS433, F401
        user, device, credential, config, drift, audit,
        revoked_token,
    )
    Base.metadata.create_all(engine)

    from fastapi import FastAPI
    from app.api.v1 import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")

    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )

    def _override_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    return app, engine


def _mint_token(user_id: int) -> str:
    os.environ.setdefault("NAW_SECRET_KEY", "test-secret-for-export-drift-only")
    from app.core.security import create_access_token
    return create_access_token(subject=user_id, extra={"role": "admin"})


class ExportDriftE2ETests(unittest.TestCase):
    def setUp(self):
        self.app, self.engine = _make_app()
        self.client = TestClient(self.app)
        SessionLocal = sessionmaker(bind=self.engine, autoflush=False, future=True)
        db = SessionLocal()

        # Create an admin user we can impersonate
        from app.models.user import User
        from app.models.device import Device
        from app.models.drift import ConfigDrift

        u = User(
            username="admin",
            password_hash="x",  # value doesn't matter, we mint JWT directly
            role="admin",
            is_active=True,
        )
        db.add(u)
        db.flush()

        d = Device(
            name="drift-device",
            vendor="huawei_vrp",
            is_simulated=True,
            is_active=True,
        )
        db.add(d)
        db.flush()

        drift = ConfigDrift(
            device_id=d.id,
            baseline_revision_id=None,
            current_revision_id=None,
            severity="warning",
            status="new",
            lines_added=3,
            lines_removed=1,
            lines_modified=2,
            baseline_hash="a" * 40,
            current_hash="b" * 40,
            diff_snippet="--- before\n+++ after\n@@ -1 +1 @@\n-old\n+new",
            change_hint="manual-edit",
            detected_at=datetime.now(timezone.utc),
        )
        db.add(drift)
        db.commit()
        self.user_id = u.id
        self.drift_id = drift.id
        db.close()

    def test_export_drift_json_does_not_500(self):
        token = _mint_token(self.user_id)
        r = self.client.get(
            "/api/v1/export/drift?format=json",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Wire shape is {scope, exported_at, exported_by, records: [...]}
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("scope"), "drift")
        self.assertIn("records", body)
        records = body["records"]
        self.assertIsInstance(records, list)
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual(row["id"], self.drift_id)
        # The fields the broken handler asked for must NOT appear.
        self.assertNotIn("current_device_id", row)
        # The fields the fixed handler emits must appear.
        for key in (
            "device_id",
            "severity",
            "status",
            "lines_added",
            "lines_removed",
            "lines_modified",
            "baseline_hash",
            "current_hash",
            "diff_snippet",
            "detected_at",
        ):
            self.assertIn(key, row, f"missing key {key}")
        self.assertEqual(row["lines_added"], 3)
        self.assertEqual(row["lines_removed"], 1)
        self.assertEqual(row["change_hint"], "manual-edit")

    def test_export_drift_requires_auth(self):
        r = self.client.get("/api/v1/export/drift?format=json")
        self.assertIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
