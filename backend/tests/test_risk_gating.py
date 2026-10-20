"""End-to-end tests for the create-job risk gating (backlog item B2).

We assert the contract for jobs whose AI risk assessment layer fails:

* The deterministic rule-based verdict is ALWAYS written before
  ``create_job`` returns. ``Job.requires_approval`` reflects the
  rule layer, never the AI layer.
* An AI/graph outage never nullifies a dangerous verdict. The job
  still requires admin approval.
* ``start_job`` refuses to run a job whose ``risk_assessment`` is
  missing entirely (defence in depth).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_app():
    """Spin up a FastAPI app backed by a throwaway SQLite DB.

    Mirrors ``test_jobs_dry_run_e2e`` so the test stack matches the
    production wiring (router → service → DB).
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-b2-only"
    os.environ["NAW_BOOTSTRAP_ADMIN_USERNAME"] = "admin"
    os.environ["NAW_BOOTSTRAP_ADMIN_PASSWORD"] = "adminpw"

    from app.core.config import settings  # noqa: WPS433
    from app.db.session import Base, get_db  # noqa: WPS433

    settings.database_url = db_url
    engine = create_engine(db_url, future=True, connect_args={"check_same_thread": False})
    from app.models import (  # noqa: WPS433, F401
        revoked_token, user, device, credential, template,
        job, drift, ai, ai_alert, document, audit, account,
        adapter, config,
    )
    Base.metadata.create_all(engine)

    # Rebind engine so the API + AI services hit the same DB the tests see.
    import app.db.session as _sm
    _sm.engine = engine
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    _sm.SessionLocal = TestingSessionLocal
    import app.workers.tasks as _tasks
    _tasks.SessionLocal = TestingSessionLocal

    from fastapi import FastAPI
    from app.api.v1 import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app, TestingSessionLocal, tmp.name


def _mint_headers(SessionLocal) -> dict[str, str]:
    """Create an admin user + mint a JWT directly.

    Bypasses ``/login`` because the test sandbox has bcrypt 5.x which is
    incompatible with passlib 1.7's self-test (see test_jobs_dry_run_e2e).
    """
    from app.models.user import User
    from app.core.security import create_access_token
    with SessionLocal() as db:
        u = User(
            username="admin",
            password_hash="!test-no-login!",
            role="admin",
            is_active=True,
        )
        db.add(u); db.commit(); db.refresh(u)
        token = create_access_token(subject=u.id, extra={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def _add_simulated_device(db) -> int:
    from app.models.device import Device
    d = Device(name="sim-dev", vendor="simulated", is_simulated=True)
    db.add(d); db.commit(); db.refresh(d)
    return d.id


class RiskGatingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.SessionLocal, _ = _make_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)
        # Reset the per-user batch-job limiter between tests so a
        # previous suite's POSTs don't bleed into this one and surface
        # as 429. The limiter is process-local so clearing its dict is
        # sufficient.
        try:
            from app.core.ratelimit import batch_job_user_limiter
            batch_job_user_limiter.reset()
        except Exception:  # noqa: BLE001
            pass
        with self.SessionLocal() as db:
            # Fresh tables per test (cascades to dependent rows).
            from app.models.job import Job, JobStep
            from app.models.device import Device
            from app.models.user import User
            db.query(JobStep).delete()
            db.query(Job).delete()
            db.query(Device).delete()
            db.query(User).delete()
            db.commit()
        self.headers = _mint_headers(self.SessionLocal)
        with self.SessionLocal() as db:
            # Fresh tables per test.
            from app.models.job import Job, JobStep
            from app.models.device import Device
            db.query(JobStep).delete()
            db.query(Job).delete()
            db.query(Device).delete()
            db.commit()

    # ── Tests ───────────────────────────────────────────────────────────

    def test_dangerous_inline_commands_require_approval(self) -> None:
        """Rule layer says dangerous → requires_approval=True even when
        AI would have failed (it does fail here: no API key configured)."""
        with self.SessionLocal() as db:
            dev_id = _add_simulated_device(db)

        resp = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "dangerous",
                "kind": "batch_write",
                "target_device_ids": [dev_id],
                "params": {"__inline_commands": "reload"},
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertTrue(body["requires_approval"])
        self.assertEqual(body["risk_assessment"]["level"], "dangerous")
        # Rule verdict was applied (not just AI guess).
        # In tests with no AI key configured the AI phase fails and the
        # stored phase stays at "rule"; if the AI layer happens to be
        # wired in this env it would say "rule+ai". Both are acceptable
        # for this assertion — what we care about is that the rule
        # verdict is authoritative for ``requires_approval``.
        self.assertIn(body["risk_assessment"].get("phase"), {"rule", "rule+ai"})

    def test_safe_inline_commands_do_not_require_approval(self) -> None:
        with self.SessionLocal() as db:
            dev_id = _add_simulated_device(db)

        resp = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "safe",
                "kind": "batch_write",
                "target_device_ids": [dev_id],
                "params": {"__inline_commands": "interface Loopback0\n ip address 9.9.9.9 32"},
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertFalse(body["requires_approval"])
        self.assertEqual(body["risk_assessment"]["level"], "safe")

    def test_start_blocked_without_approval_on_dangerous_job(self) -> None:
        with self.SessionLocal() as db:
            dev_id = _add_simulated_device(db)

        create = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "dangerous",
                "kind": "batch_write",
                "target_device_ids": [dev_id],
                "params": {"__inline_commands": "write erase"},
            },
        )
        job_id = create.json()["id"]

        resp = self.client.post(f"/api/v1/jobs/{job_id}/start", headers=self.headers)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("HIGH-RISK", resp.json()["detail"])

    def test_start_refuses_job_without_risk_assessment(self) -> None:
        """Defence-in-depth: if a Job row has no risk_assessment at all,
        start_job must refuse (don't fall through to the worker)."""
        from app.models.job import Job

        with self.SessionLocal() as db:
            dev_id = _add_simulated_device(db)
            job = Job(
                name="orphan",
                kind="batch_write",
                target_device_ids=[dev_id],
                params={"__inline_commands": "interface Loopback0"},
                initiated_by="admin",
                status="draft",
                risk_assessment=None,  # deliberately null
                requires_approval=False,
            )
            db.add(job); db.commit(); db.refresh(job)
            job_id = job.id

        resp = self.client.post(f"/api/v1/jobs/{job_id}/start", headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("risk assessment", resp.json()["detail"])

    def test_dry_run_failure_returns_400(self) -> None:
        """If dry_run can't render (e.g. missing template), the API now
        surfaces a 4xx instead of swallowing it and storing a partial
        Job row."""
        with self.SessionLocal() as db:
            dev_id = _add_simulated_device(db)

        resp = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "name": "broken template",
                "kind": "template_apply",
                "target_device_ids": [dev_id],
                "template_id": 999_999,
                "params": {},
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("dry-run failed", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
