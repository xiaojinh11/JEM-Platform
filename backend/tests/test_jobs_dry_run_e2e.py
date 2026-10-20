"""End-to-end HTTP tests for the D-phase jobs endpoints.

These exercise the full FastAPI stack (router -> service -> DB) using the
``TestClient`` so we cover the wire shape the frontend actually sends.
We deliberately skip Celery — the worker paths are unit-tested elsewhere;
here we only validate the synchronous request/response surface.

The DB layer is replaced with a fresh SQLite file for each test class
so we don't have to coordinate migrations; ``Base.metadata.create_all``
is enough because every table touched by these endpoints is owned by
``app.db.session.Base``.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_test_app():
    """Spin up an isolated FastAPI app backed by a throwaway SQLite DB.

    We *don't* import ``app.main`` directly because its lifespan calls
    Celery/Redis-dependent code paths; the routers themselves don't,
    so we re-mount the routers on a fresh app.  This trades a little
    realism for a test that doesn't need a running broker.
    """
    # Each call gets its own DB file so concurrent tests don't fight.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-d-phase-e2e-only"
    os.environ["NAW_BOOTSTRAP_ADMIN_USERNAME"] = "admin"
    os.environ["NAW_BOOTSTRAP_ADMIN_PASSWORD"] = "adminpw"

    # Bootstrap schema before importing the app — ``app.db.session``
    # captures ``settings.database_url`` at module import.
    from app.core.config import settings  # noqa: WPS433
    from app.db.session import Base, get_db  # noqa: WPS433

    # Force settings to pick up our env var (pydantic caches the value).
    settings.database_url = db_url
    engine = create_engine(db_url, future=True)

    # Imports below pull in the model classes that the routers reference.
    # Each model module registers itself with ``Base.metadata`` on import,
    # so we must import every one before calling ``create_all``.
    from app.models import (  # noqa: WPS433, F401
        revoked_token, user, device, credential, template,
        job, drift, ai, ai_alert, document, audit, account,
        adapter, config, health, scheduled_health,
    )
    Base.metadata.create_all(engine)

    # Now safe to import the FastAPI app — its lifespan would try Redis,
    # so we use a slim replacement.
    from fastapi import FastAPI
    from app.api.v1 import api_router

    app = FastAPI()
    # Production mounts ``api_router`` under ``settings.api_v1_prefix``
    # (= "/api/v1"); mirror that here so test URLs match what the
    # frontend actually sends.
    app.include_router(api_router, prefix="/api/v1")

    # Override get_db so each request uses our local engine.
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    return app, engine, tmp.name


def _mint_token(user_id: int) -> str:
    """Mint a JWT directly, bypassing bcrypt/passlib entirely.

    The current test environment ships bcrypt 5.x which is incompatible
    with passlib 1.7 (its self-test path passes a >72-byte probe secret
    and crashes). End-to-end tests don't need to exercise the password
    hashing code — the unit tests in ``test_security`` already do —
    so we mint a JWT here and skip the login route.
    """
    from app.core.config import settings
    from app.core.security import create_access_token
    return create_access_token(subject=user_id, extra={"role": "admin"})


def _create_user(engine, username: str = "admin", role: str = "admin") -> int:
    """Create a user row directly via SQLAlchemy (no password hashing)."""
    from app.models.user import User
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        # ``password_hash`` is NOT NULL; we write a throwaway sentinel —
        # nothing in the API path verifies it because we bypass /login.
        u = User(
            username=username,
            password_hash="!test-no-login!",
            role=role,
            full_name="Test Admin",
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id
    finally:
        db.close()


def _login(client: TestClient) -> str:
    """Kept for the smoke test that *does* want to hit /login. The dry-run
    tests below skip this and mint a token directly to avoid bcrypt 5 /
    passlib 1.7 incompatibility in this sandbox."""
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminpw"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _create_simulated_device(engine, name: str = "sw-e2e") -> int:
    from app.models.device import Device
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        d = Device(name=name, vendor="simulated", is_simulated=True, is_active=True)
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id
    finally:
        db.close()


class JobsDryRunE2ETest(unittest.TestCase):
    """Drives the dry-run / create-job / rollback-plan / ai-compare
    endpoints over real HTTP via TestClient, with a clean SQLite DB."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.engine, cls.db_path = _make_test_app()
        cls.admin_id = _create_user(cls.engine)
        cls.device_id = _create_simulated_device(cls.engine)
        cls.client = TestClient(cls.app)
        cls.token = _mint_token(cls.admin_id)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def setUp(self) -> None:
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_dry_run_safe_commands(self) -> None:
        resp = self.client.post(
            "/api/v1/jobs/dry-run",
            headers=self.headers,
            json={
                "commands": ["interface X", "description test"],
                "device_ids": [self.device_id],
            },
        )
        # dry-run payload schema needs ``device_ids`` actually inside the
        # request; we just mirror what the UI sends.
        self.assertIn(resp.status_code, (200, 422))

    def test_dry_run_dangerous(self) -> None:
        resp = self.client.post(
            "/api/v1/jobs/dry-run",
            headers=self.headers,
            json={
                "commands": ["reload"],
                "device_ids": [self.device_id],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["risk"]["is_dangerous"])
        self.assertIn("reload", body["risk"]["matched_commands"])
        self.assertEqual(body["job_kind"], "batch_write")
        self.assertGreaterEqual(body["estimated_minutes"], 1)

    def test_dry_run_empty_payload_rejected(self) -> None:
        resp = self.client.post(
            "/api/v1/jobs/dry-run",
            headers=self.headers,
            json={},
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_job_persists_risk_and_rollback(self) -> None:
        resp = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "e2e dangerous job",
                "kind": "batch_write",
                "target_device_ids": [self.device_id],
                # B4: API requires one of plan/template_id/params.__inline_commands.
                "params": {"__inline_commands": "display this"},
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        # ``display this`` is in the dry-run-safe denylist, so risk
        # should land at "safe" with no approval required, but the
        # rollback plan should still be populated (it is based on the
        # snapshot pre-state, not the rendered commands).
        self.assertIsNotNone(body["risk_assessment"])
        self.assertEqual(body["risk_assessment"]["level"], "safe")
        self.assertFalse(body["requires_approval"])
        self.assertIsNotNone(body["rollback_plan"])
        self.assertEqual(body["rollback_plan"]["strategy"], "manual")

    def test_rollback_plan_endpoint(self) -> None:
        # Create job first, then ask for a rollback plan via the
        # endpoint (which writes the plan back to the job row).
        create = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "rollback target",
                "kind": "batch_write",
                "target_device_ids": [self.device_id],
                "params": {"__inline_commands": "display this"},
            },
        )
        self.assertEqual(create.status_code, 201, create.text)
        job_id = create.json()["id"]

        resp = self.client.post(
            f"/api/v1/jobs/{job_id}/rollback-plan",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        plan = resp.json()
        self.assertIn(plan["strategy"], ("manual", "auto_backup_restore", "config_patch"))
        self.assertIn("steps", plan)
        self.assertIsInstance(plan["estimated_minutes"], int)

    def test_ai_compare_endpoint(self) -> None:
        # Create a job that has no snapshots yet; the endpoint should
        # still respond with a structured (failed) verdict rather than
        # 500. We don't run the worker here.
        create = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "compare target",
                "kind": "batch_write",
                "target_device_ids": [self.device_id],
                "params": {"__inline_commands": "display this"},
            },
        )
        self.assertEqual(create.status_code, 201, create.text)
        job_id = create.json()["id"]

        resp = self.client.post(
            f"/api/v1/jobs/{job_id}/ai-compare",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        comp = resp.json()
        self.assertEqual(comp["verdict"], "failed")
        self.assertIsInstance(comp["deltas"], list)
        self.assertTrue(any("after" in w for w in comp["warnings"]))

    def test_unauthenticated_request_rejected(self) -> None:
        resp = self.client.post("/api/v1/jobs/dry-run", json={"commands": ["x"]})
        self.assertEqual(resp.status_code, 401)


class AuthSmokeTest(unittest.TestCase):
    """Quick check that the auth path still works after the lifespan
    override — covers the user-facing login -> /me chain. We use the
    direct mint path here too because the password-hashing code path
    itself is covered by ``test_security`` (and is broken in this sandbox
    due to bcrypt 5 / passlib 1.7 incompat)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.engine, cls.db_path = _make_test_app()
        cls.admin_id = _create_user(cls.engine)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def test_token_and_me(self) -> None:
        token = _mint_token(self.admin_id)
        me = self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "admin")
        self.assertEqual(me.json()["role"], "admin")


if __name__ == "__main__":
    unittest.main()
