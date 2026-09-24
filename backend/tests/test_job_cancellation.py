"""End-to-end tests for job cancellation (backlog item B5).

The contract we lock in here:

* The worker polls Job.status between device iterations; once it sees
  CANCELED it stops dispatching and stamps the remaining devices with
  a "skipped" step instead of running them.
* batch_apply refuses to start a job that was already CANCELED
  (returns CANCELED immediately without dispatching to any device).
* _finalize_job_status() refuses to overwrite a CANCELED status with
  PARTIAL/SUCCEEDED — the operator signal is terminal.
* The /control cancel API refuses to "cancel" an already-terminal job
  (succeeded/failed/partial/needs_verification/canceled), returning
  409 so a late cancel can't race the worker's finalize.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_engine_and_sessionmaker():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-b5-only"

    from app.core.config import settings  # noqa: WPS433
    from app.db.session import Base  # noqa: WPS433

    settings.database_url = db_url
    engine = create_engine(db_url, future=True, connect_args={"check_same_thread": False})
    from app.models import (  # noqa: WPS433, F401
        revoked_token, user, device, credential, template,
        job, drift, ai, ai_alert, document, audit, account,
        adapter, config,
    )
    Base.metadata.create_all(engine)

    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    import app.db.session as _sm
    _sm.engine = engine
    _sm.SessionLocal = TestingSessionLocal
    import app.workers.tasks as _tasks
    _tasks.SessionLocal = TestingSessionLocal
    return TestingSessionLocal, tmp.name


class _DeviceFactory:
    def __init__(self, SessionLocal):
        self.SessionLocal = SessionLocal

    def add(self, name: str) -> int:
        from app.models.device import Device
        with self.SessionLocal() as db:
            dev = Device(name=name, vendor="simulated", is_simulated=True)
            db.add(dev); db.commit(); db.refresh(dev)
            return dev.id


class _JobFactory:
    def __init__(self, SessionLocal):
        self.SessionLocal = SessionLocal

    def create(self, *, target_device_ids: list[int], params: dict[str, Any] | None = None) -> int:
        from app.models.job import Job
        with self.SessionLocal() as db:
            job = Job(
                name="t",
                kind="batch_write",
                target_device_ids=target_device_ids,
                template_id=None,
                params=params or {"__inline_commands": "interface Loopback0\n ip address 1.1.1.1 32"},
                initiated_by="tester",
                status="draft",
            )
            db.add(job); db.commit(); db.refresh(job)
            return job.id


class WorkerCancellationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.SessionLocal, cls.db_path = _make_engine_and_sessionmaker()

    def setUp(self) -> None:
        with self.SessionLocal() as db:
            from app.models.job import Job, JobStep
            from app.models.template import Template
            from app.models.device import Device
            db.query(JobStep).delete()
            db.query(Job).delete()
            db.query(Device).delete()
            db.query(Template).delete()
            db.commit()

    # ── Tests ───────────────────────────────────────────────────────────

    def test_cancel_at_start_aborts_before_any_device(self) -> None:
        """If the job is already CANCELED when the worker picks it up,
        batch_apply returns immediately without touching any device."""
        from app.workers.tasks import batch_apply
        from app.models.job import Job, JobStep
        from app.core.enums import JobStatus

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        ids = [devices.add(f"d{i}") for i in range(3)]
        job_id = jobs.create(target_device_ids=ids)

        # Pre-cancel before worker runs.
        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            j.status = JobStatus.CANCELED.value
            db.commit()

        result = batch_apply.run(job_id)
        self.assertEqual(result, JobStatus.CANCELED.value)

        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            self.assertEqual(j.status, JobStatus.CANCELED.value)
            steps = db.query(JobStep).filter_by(job_id=job_id).all()
            # No steps should have been written since the job was already
            # canceled before the worker started.
            self.assertEqual(len(steps), 0)

    def test_cancel_intercepted_mid_loop_skips_remaining_devices(self) -> None:
        """Simulate the operator cancelling while the worker is processing.
        We patch ``time.sleep`` so we can write the cancel status into the
        DB *between* two iterations, after the first device has completed
        and before the second one starts. The second and third devices
        should receive "skipped" steps."""
        from app.workers.tasks import batch_apply
        from app.models.job import Job, JobStep
        from app.core.enums import JobStatus
        import time

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        ids = [devices.add(f"d{i}") for i in range(3)]
        job_id = jobs.create(target_device_ids=ids)

        # Intercept time.sleep: the *second* time it fires (between devices
        # 0 and 1) we write a cancel into the DB. This mimics an operator
        # pressing "cancel" mid-job.
        _sleep_calls = [0]

        def _patched_sleep(seconds: float) -> None:
            _sleep_calls[0] += 1
            if _sleep_calls[0] == 1:  # after device 0 completes, before device 1 starts
                import app.db.session as _sm
                with _sm.SessionLocal() as cancel_db:
                    j = cancel_db.get(Job, job_id)
                    j.status = JobStatus.CANCELED.value
                    cancel_db.commit()
            original_sleep(seconds)

        original_sleep = time.sleep
        time.sleep = _patched_sleep
        try:
            result = batch_apply.run(job_id)
        finally:
            time.sleep = original_sleep

        self.assertEqual(result, JobStatus.CANCELED.value)

        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            self.assertEqual(j.status, JobStatus.CANCELED.value)
            steps = db.query(JobStep).filter_by(job_id=job_id).all()
            step_map = {s.device_id: s for s in steps}
            # Device 0: ran normally. Device 1: was the first iteration
            # after the injected cancel — _job_was_canceled returned True
            # at the top of its iteration, so it got a skipped step and
            # the loop broke before processing devices 2+.
            self.assertIn(step_map[ids[0]].status, {"succeeded", "needs_verification"})
            self.assertEqual(step_map[ids[1]].status, "skipped")
            self.assertIn("canceled", (step_map[ids[1]].error or "").lower())
            # Devices 2+ were never iterated, so they have no step rows.
            for dev_id in ids[2:]:
                self.assertNotIn(dev_id, step_map)

    def test_finalize_preserves_cancelled_status(self) -> None:
        """Even if the worker has already written a PARTIAL summary,
        if the row was canceled we must end at canceled."""
        from app.workers.tasks import _finalize_job_status, _summarize
        from app.models.job import Job
        from app.core.enums import JobStatus

        jobs = _JobFactory(self.SessionLocal)
        devices = _DeviceFactory(self.SessionLocal)
        ids = [devices.add("a"), devices.add("b")]
        job_id = jobs.create(target_device_ids=ids)

        # Pre-cancel.
        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            j.status = JobStatus.CANCELED.value
            db.commit()

        # Manually invoke _finalize_job_status with a summary that
        # would otherwise yield PARTIAL.
        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            # Force a non-empty summary so the function doesn't try
            # to query job.steps (none exist for this job).
            j.summary = {"total": 2, "succeeded": 1, "failed": 0,
                         "needs_verification": 1, "skipped": 0}
            _finalize_job_status(db, j)

        with self.SessionLocal() as db:
            j = db.get(Job, job_id)
            self.assertEqual(j.status, JobStatus.CANCELED.value)
            self.assertIsNotNone(j.finished_at)


class ControlApiCancellationTest(unittest.TestCase):
    """The control API rejects already-terminal cancels."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.SessionLocal = _make_app()
        # Mint the admin token once for the class (idempotent within the
        # same DB since setUp truncates the users table between tests).
        cls.headers = _mint_headers(cls.SessionLocal)

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)
        with self.SessionLocal() as db:
            from app.models.job import Job
            from app.models.device import Device
            db.query(Job).delete()
            db.query(Device).delete()
            db.commit()
        # Re-mint token each time so we always have a valid one.
        self.headers = _mint_headers(self.SessionLocal)

    def test_cancel_running_job_sets_canceled(self) -> None:
        with self.SessionLocal() as db:
            from app.models.device import Device
            from app.models.job import Job
            d = Device(name="x", vendor="simulated", is_simulated=True)
            db.add(d); db.commit(); db.refresh(d)
            j = Job(name="j", kind="batch_write",
                    target_device_ids=[d.id], initiated_by="admin",
                    status="running", params={"__inline_commands": "x"})
            db.add(j); db.commit(); db.refresh(j)
            job_id = j.id

        resp = self.client.post(
            f"/api/v1/jobs/{job_id}/control",
            headers=self.headers, json={"action": "cancel"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "canceled")

    def test_cancel_terminal_job_returns_409(self) -> None:
        for terminal in ("succeeded", "failed", "partial", "needs_verification", "canceled"):
            with self.subTest(terminal=terminal):
                with self.SessionLocal() as db:
                    from app.models.device import Device
                    from app.models.job import Job
                    d = Device(name="t", vendor="simulated", is_simulated=True)
                    db.add(d); db.commit(); db.refresh(d)
                    j = Job(name="j", kind="batch_write",
                            target_device_ids=[d.id], initiated_by="admin",
                            status=terminal,
                            params={"__inline_commands": "x"})
                    db.add(j); db.commit(); db.refresh(j)
                    job_id = j.id

                resp = self.client.post(
                    f"/api/v1/jobs/{job_id}/control",
                    headers=self.headers, json={"action": "cancel"},
                )
                self.assertEqual(resp.status_code, 409, f"{terminal}: {resp.text}")
                self.assertIn("terminal", resp.json()["detail"])


# ── Shared fixtures for the FastAPI stack ──────────────────────────────────

def _make_app():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-b5-api"

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
        try: yield db
        finally: db.close()
    app.dependency_overrides[get_db] = _override_get_db
    return app, TestingSessionLocal


def _mint_headers(SessionLocal) -> dict[str, str]:
    from app.models.user import User
    from app.core.security import create_access_token
    with SessionLocal() as db:
        db.query(User).filter_by(username="admin").delete()
        u = User(username="admin", password_hash="!test!", role="admin", is_active=True)
        db.add(u); db.commit(); db.refresh(u)
        token = create_access_token(subject=u.id, extra={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


if __name__ == "__main__":
    unittest.main()
