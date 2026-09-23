"""E2E coverage for the export endpoints after the model rename.

Originally we referenced ``ConfigDrift.current_device_id`` and several
fields on ``ConfigRevision`` that don't exist on the model. The fixes
live in ``app.api.v1.export._drift`` and ``_configs``. This test guards
against regressions so a future column rename doesn't crash the JSON
endpoints with 500.
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
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-export-only"
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
    os.environ.setdefault("NAW_SECRET_KEY", "test-secret-for-export-only")
    from app.core.security import create_access_token
    return create_access_token(subject=user_id, extra={"role": "admin"})


def _seed_drift(engine) -> int:
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    db = SessionLocal()
    try:
        from app.models.user import User
        from app.models.device import Device
        from app.models.drift import ConfigDrift

        u = User(
            username="admin",
            password_hash="x",
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
            severity="warning",
            status="new",
            lines_added=3,
            lines_removed=1,
            lines_modified=2,
            baseline_hash="a" * 40,
            current_hash="b" * 40,
            diff_snippet="--- before\n+++ after\n-old\n+new",
            change_hint="manual-edit",
            detected_at=datetime.now(timezone.utc),
        )
        db.add(drift)
        db.commit()
        return u.id, drift.id
    finally:
        db.close()


def _seed_config(engine) -> tuple[int, int]:
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    db = SessionLocal()
    try:
        from app.models.user import User
        from app.models.device import Device
        from app.models.config import ConfigArchive, ConfigRevision

        u = User(
            username="admin",
            password_hash="x",
            role="admin",
            is_active=True,
        )
        db.add(u)
        db.flush()
        d = Device(
            name="cfg-device",
            vendor="huawei_vrp",
            is_simulated=True,
            is_active=True,
        )
        db.add(d)
        db.flush()
        archive = ConfigArchive(device_id=d.id)
        db.add(archive)
        db.flush()
        rev = ConfigRevision(
            archive_id=archive.id,
            revision_no=1,
            source="collect",
            author="admin",
            note="baseline",
            content_hash="c" * 40,
            size_bytes=2048,
            storage_path="/tmp/cfg.cfg",
        )
        db.add(rev)
        db.commit()
        return u.id, rev.id
    finally:
        db.close()


class ExportDriftE2ETests(unittest.TestCase):
    def setUp(self):
        self.app, self.engine = _make_app()
        self.client = TestClient(self.app)
        self.user_id, self.drift_id = _seed_drift(self.engine)

    def test_export_drift_json_does_not_500(self):
        token = _mint_token(self.user_id)
        r = self.client.get(
            "/api/v1/export/drift?format=json",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("scope"), "drift")
        records = body.get("records")
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
        self.assertEqual(row["change_hint"], "manual-edit")

    def test_export_drift_requires_auth(self):
        r = self.client.get("/api/v1/export/drift?format=json")
        self.assertIn(r.status_code, (401, 403))


class ExportConfigsE2ETests(unittest.TestCase):
    def setUp(self):
        self.app, self.engine = _make_app()
        self.client = TestClient(self.app)
        self.user_id, self.rev_id = _seed_config(self.engine)

    def test_export_configs_json_does_not_500(self):
        token = _mint_token(self.user_id)
        r = self.client.get(
            "/api/v1/export/configs?format=json",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("scope"), "configs")
        records = body.get("records")
        self.assertIsInstance(records, list)
        self.assertEqual(len(records), 1)
        row = records[0]
        # The fields the broken handler asked for must NOT appear.
        for bad in ("revision", "config_hash", "config_preview", "config_text"):
            self.assertNotIn(bad, row)
        # The fields the fixed handler emits must appear.
        for key in (
            "archive_id",
            "device_id",
            "revision_no",
            "source",
            "author",
            "note",
            "size_bytes",
            "content_hash",
            "storage_path",
            "created_at",
        ):
            self.assertIn(key, row, f"missing key {key}")
        self.assertEqual(row["revision_no"], 1)
        self.assertEqual(row["size_bytes"], 2048)

    def test_export_configs_empty_db_returns_empty_records(self):
        """An empty DB shouldn't crash with 500 — it should return an
        empty ``records`` list.  We seed a separate engine for this."""
        empty_app, empty_engine = _make_app()
        empty_client = TestClient(empty_app)
        # No seeding → 0 devices, 0 revisions.
        SessionLocal = sessionmaker(bind=empty_engine, autoflush=False, future=True)
        db = SessionLocal()
        try:
            from app.models.user import User

            u = User(
                username="admin",
                password_hash="x",
                role="admin",
                is_active=True,
            )
            db.add(u)
            db.commit()
            user_id = u.id
        finally:
            db.close()
        token = _mint_token(user_id)
        r = empty_client.get(
            "/api/v1/export/configs?format=json",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["scope"], "configs")
        self.assertEqual(body["records"], [])


if __name__ == "__main__":
    unittest.main()

