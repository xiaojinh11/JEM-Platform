"""End-to-end tests for the worker-side job apply path.

We exercise ``batch_apply`` directly (Celery ``.delay`` is bypassed via
``.run()`` which is the synchronous entry-point Celery uses internally)
so we can assert on the resulting Job + JobStep rows without needing a
broker. The goal is to lock in the fixes for backlog item B1:

* The worker must read the template + params from the Job row, not from
  legacy API-supplied args.
* A rendered command list of length zero must produce a ``failed``
  PRECHECK step — never a spurious ``succeeded``.
* A missing or empty Template row must produce a ``failed`` PRECHECK
  step per device — never a spurious ``succeeded``.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_engine_and_sessionmaker():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["NAW_SECRET_KEY"] = "test-secret-for-worker-apply-only"

    from app.core.config import settings  # noqa: WPS433
    from app.db.session import Base  # noqa: WPS433

    settings.database_url = db_url
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        db_url,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.models import (  # noqa: WPS433, F401
        revoked_token, user, device, credential, template,
        job, drift, ai, ai_alert, document, audit, account,
        adapter, config,
    )
    Base.metadata.create_all(engine)

    # IMPORTANT: rebind the module-level ``engine`` AND ``SessionLocal`` so
    # any code that did ``from app.db.session import SessionLocal`` at
    # import time (like ``app.workers.tasks``) now sees our test factory.
    # We rebind both the source module and the already-imported names in
    # ``app.workers.tasks``.
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    import app.db.session as _sm
    _sm.engine = engine
    _sm.SessionLocal = TestingSessionLocal
    import app.workers.tasks as _tasks
    _tasks.SessionLocal = TestingSessionLocal

    return TestingSessionLocal, tmp.name


class _DeviceFactory:
    def __init__(self, SessionLocal):
        self.SessionLocal = SessionLocal

    def add(self) -> int:
        from app.models.device import Device
        with self.SessionLocal() as db:
            dev = Device(name="sim-dev", vendor="simulated", is_simulated=True)
            db.add(dev)
            db.commit()
            return dev.id


class _JobFactory:
    def __init__(self, SessionLocal):
        self.SessionLocal = SessionLocal

    def create(
        self,
        *,
        target_device_ids: list[int],
        template_id: int | None = None,
        params: dict[str, Any] | None = None,
        kind: str = "template_apply",
    ) -> int:
        from app.models.job import Job
        with self.SessionLocal() as db:
            job = Job(
                name="t",
                kind=kind,
                target_device_ids=target_device_ids,
                template_id=template_id,
                params=params or {},
                initiated_by="tester",
                status="draft",
            )
            db.add(job)
            db.commit()
            return job.id

    def add_template(self, name: str, body: str) -> int:
        from app.models.template import Template
        with self.SessionLocal() as db:
            tpl = Template(name=name, body=body, description="test", created_by="tester")
            db.add(tpl)
            db.commit()
            return tpl.id


class WorkerApplyEmptyPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.SessionLocal, cls.db_path = _make_engine_and_sessionmaker()

    def setUp(self) -> None:
        # Clean slate between tests so we don't see leftover jobs / steps.
        with self.SessionLocal() as db:
            from app.models.job import Job, JobStep
            from app.models.template import Template
            db.query(JobStep).delete()
            db.query(Job).delete()
            db.query(Template).delete()
            db.commit()

    # ── Tests ───────────────────────────────────────────────────────────

    def test_empty_rendered_template_marks_precheck_failed(self) -> None:
        from app.workers.tasks import batch_apply
        from app.models.job import Job, JobStep

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        dev_id = devices.add()
        tpl_id = jobs.add_template(name="empty", body="# only a comment, nothing to apply\n")
        job_id = jobs.create(target_device_ids=[dev_id], template_id=tpl_id)

        result = batch_apply.run(job_id)
        self.assertEqual(result, "failed")

        with self.SessionLocal() as db:
            job = db.get(Job, job_id)
            self.assertEqual(job.status, "failed")
            prechecks = [s for s in db.query(JobStep).filter_by(job_id=job_id).all() if s.step_kind == "precheck"]
            self.assertEqual(len(prechecks), 1)
            self.assertEqual(prechecks[0].status, "failed")
            self.assertIn("0 commands", prechecks[0].error or "")

    def test_missing_template_marks_precheck_failed(self) -> None:
        from app.workers.tasks import batch_apply
        from app.models.job import Job, JobStep

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        dev_id = devices.add()
        job_id = jobs.create(target_device_ids=[dev_id], template_id=999_999)

        result = batch_apply.run(job_id)
        self.assertEqual(result, "failed")

        with self.SessionLocal() as db:
            job = db.get(Job, job_id)
            self.assertEqual(job.status, "failed")
            prechecks = [s for s in db.query(JobStep).filter_by(job_id=job_id).all() if s.step_kind == "precheck"]
            self.assertEqual(len(prechecks), 1)
            self.assertEqual(prechecks[0].status, "failed")
            self.assertIn("not found", prechecks[0].error or "")

    def test_no_template_no_inline_commands_marks_failed(self) -> None:
        from app.workers.tasks import batch_apply
        from app.models.job import Job

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        dev_id = devices.add()
        job_id = jobs.create(target_device_ids=[dev_id], template_id=None, params={})

        result = batch_apply.run(job_id)
        self.assertEqual(result, "failed")
        with self.SessionLocal() as db:
            job = db.get(Job, job_id)
            self.assertEqual(job.status, "failed")

    def test_inline_commands_render_and_succeed(self) -> None:
        """Happy path: inline commands survive the round-trip."""
        from app.workers.tasks import batch_apply
        from app.models.job import Job, JobStep

        devices = _DeviceFactory(self.SessionLocal)
        jobs = _JobFactory(self.SessionLocal)

        dev_id = devices.add()
        job_id = jobs.create(
            target_device_ids=[dev_id],
            template_id=None,
            params={"__inline_commands": "interface Loopback0\n ip address 1.1.1.1 32"},
            kind="batch_write",
        )

        batch_apply.run(job_id)

        with self.SessionLocal() as db:
            job = db.get(Job, job_id)
            # Simulated devices land in ``needs_verification`` once commands
            # actually got pushed. The happy path skips an explicit
            # PRECHECK step (only the failure path writes one).
            self.assertIn(job.status, {"needs_verification", "succeeded", "partial"})
            all_steps = db.query(JobStep).filter_by(job_id=job_id).all()
            execute_steps = [s for s in all_steps if s.step_kind == "execute"]
            self.assertTrue(
                execute_steps,
                f"no execute step recorded; steps={[s.step_kind + ':' + s.status for s in all_steps]}",
            )
            self.assertIn(execute_steps[0].status, {"succeeded", "needs_verification"})


if __name__ == "__main__":
    unittest.main()
