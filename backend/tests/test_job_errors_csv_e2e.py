"""End-to-end test for the per-job error CSV export endpoint.

Drives ``GET /api/v1/jobs/{id}/errors.csv`` through a fresh FastAPI
app + SQLite DB. We seed:

* one device,
* one job with three JobSteps — one succeeded, one failed, one
  ``needs_verification``.

The endpoint should return:
* HTTP 200 with ``Content-Type: text/csv; charset=utf-8``,
* a CSV body whose header row matches the published column order,
* exactly 2 data rows (succeeded is excluded),
* device_name resolved from the joined Device row.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# We borrow the test-app factory + auth helpers from the existing
# jobs e2e suite — duplicating them here would mean two copies of the
# bcrypt-5 / passlib-1.7 workaround to maintain.
from tests.test_jobs_dry_run_e2e import (  # type: ignore  # noqa: E402
    _make_test_app, _mint_token, _create_user, _create_simulated_device,
)


_EXPECTED_COLUMNS = [
    "job_id", "job_name", "job_kind", "job_status",
    "device_id", "device_name", "vendor",
    "step_kind", "step_status", "error",
    "started_at", "finished_at", "duration_ms",
]


class JobErrorsCsvE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.engine, cls.db_path = _make_test_app()
        cls.admin_id = _create_user(cls.engine)
        cls.device_id = _create_simulated_device(cls.engine)
        cls.client = TestClient(cls.app)
        cls.token = _mint_token(cls.admin_id)
        cls.headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def _seed_job_with_mixed_steps(self, name: str = "csv target") -> int:
        """Insert a Job + 3 JobSteps and return the job id.

        Step statuses (in id order):
            1. succeeded — must NOT appear in the CSV
            2. failed    — must appear
            3. needs_verification — must appear
        """
        from app.models.job import Job, JobStep

        Session = sessionmaker(bind=self.engine, future=True)
        db = Session()
        try:
            now = datetime.now(timezone.utc)
            job = Job(
                name=name,
                kind="batch_write",
                target_device_ids=[self.device_id],
                status="partial",
                initiated_by="admin",
                created_at=now,
                started_at=now,
                finished_at=now + timedelta(minutes=2),
            )
            db.add(job)
            db.flush()
            job_id = job.id

            # Step 1 — succeeded (should be filtered out)
            db.add(JobStep(
                job_id=job_id, device_id=self.device_id,
                step_kind="execute", status="succeeded",
                started_at=now, finished_at=now + timedelta(seconds=10),
                error=None,
            ))
            # Step 2 — failed (should be present)
            db.add(JobStep(
                job_id=job_id, device_id=self.device_id,
                step_kind="verify", status="failed",
                started_at=now + timedelta(seconds=15),
                finished_at=now + timedelta(seconds=45),
                error="device returned '% Unknown command'",
            ))
            # Step 3 — needs_verification (should be present)
            db.add(JobStep(
                job_id=job_id, device_id=self.device_id,
                step_kind="verify", status="needs_verification",
                started_at=now + timedelta(seconds=50),
                finished_at=now + timedelta(seconds=80),
                error="hash mismatch on line 17",
            ))
            db.commit()
            return job_id
        finally:
            db.close()

    def test_404_for_unknown_job(self) -> None:
        resp = self.client.get(
            "/api/v1/jobs/99999/errors.csv",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_rejected(self) -> None:
        resp = self.client.get(
            f"/api/v1/jobs/1/errors.csv",
        )
        # Without a token, our dep chain raises 401.
        self.assertEqual(resp.status_code, 401)

    def test_csv_has_two_rows_and_resolved_device(self) -> None:
        job_id = self._seed_job_with_mixed_steps("csv e2e")

        resp = self.client.get(
            f"/api/v1/jobs/{job_id}/errors.csv",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("text/csv", resp.headers["content-type"])

        body = resp.content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(body))
        self.assertEqual(reader.fieldnames, _EXPECTED_COLUMNS)
        rows = list(reader)

        # 2 of 3 steps are errors
        self.assertEqual(len(rows), 2, f"expected 2 error rows, got {rows}")

        statuses = sorted(r["step_status"] for r in rows)
        self.assertEqual(statuses, ["failed", "needs_verification"])

        # Device is resolved — not "(deleted#...)"
        for r in rows:
            self.assertTrue(r["device_name"], "device_name should be populated")
            self.assertNotIn("(deleted", r["device_name"])

        # Job-level metadata is consistent across rows
        for r in rows:
            self.assertEqual(r["job_id"], str(job_id))
            self.assertEqual(r["job_name"], "csv e2e")
            self.assertEqual(r["job_kind"], "batch_write")
            self.assertEqual(r["job_status"], "partial")

        # duration_ms is populated for both error steps
        for r in rows:
            self.assertGreater(int(r["duration_ms"]), 0)

        # Error text is preserved (newlines stripped to single line)
        failed_row = next(r for r in rows if r["step_status"] == "failed")
        self.assertIn("Unknown command", failed_row["error"])

        # Content-Disposition carries the filename
        cd = resp.headers.get("content-disposition", "")
        self.assertIn("attachment", cd)
        self.assertIn(f"naw-job-{job_id}", cd)
        self.assertIn(".csv", cd)

    def test_clean_job_returns_header_only(self) -> None:
        """A job with no failed steps still returns a parseable CSV — just
        the header line and zero data rows. This is how the UI signals
        'job is clean, nothing to download' without an error path."""
        from app.models.job import Job, JobStep

        Session = sessionmaker(bind=self.engine, future=True)
        db = Session()
        try:
            now = datetime.now(timezone.utc)
            job = Job(
                name="clean job",
                kind="batch_read",
                target_device_ids=[self.device_id],
                status="succeeded",
                initiated_by="admin",
                created_at=now, started_at=now, finished_at=now,
            )
            db.add(job)
            db.flush()
            db.add(JobStep(
                job_id=job.id, device_id=self.device_id,
                step_kind="execute", status="succeeded",
                started_at=now, finished_at=now,
                error=None,
            ))
            db.commit()
            job_id = job.id
        finally:
            db.close()

        resp = self.client.get(
            f"/api/v1/jobs/{job_id}/errors.csv",
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
