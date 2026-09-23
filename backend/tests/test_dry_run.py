"""Unit tests for dry-run preview, rollback plan, and AI comparison."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
# Importing these so SQLAlchemy can resolve the forward references Device
# declares (``ConfigDrift``, ``DriftBaseline``). Without them, instantiating
# a Device raises InvalidRequestError when the mapper configures itself.
from app.models.drift import ConfigDrift, DriftBaseline  # noqa: F401
from app.models.job import BackupSnapshot, Job, JobStep
from app.services.dry_run import (
    _count_diff_lines,
    build_rollback_plan,
    compute_ai_comparison,
    dry_run,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_engine() -> tuple[Any, Any]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    eng = create_engine(f"sqlite:///{tmp.name}", future=True)
    # Only create the tables this suite touches. ``Base.metadata.create_all``
    # would also try unrelated models (ai_chat_messages, etc.) and fail on
    # FK ordering when their parent tables aren't in this fresh SQLite db.
    wanted = {"jobs", "job_steps", "backup_snapshots", "devices", "templates"}
    for name in wanted:
        tbl = Base.metadata.tables.get(name)
        if tbl is not None:
            tbl.create(eng, checkfirst=True)
    return eng, tmp.name


class DryRunTest(unittest.TestCase):
    def _session(self):
        engine, _path = _make_engine()
        return sessionmaker(bind=engine, future=True)()

    def test_safe_commands_no_notes(self) -> None:
        db = self._session()
        try:
            preview = dry_run(
                db,
                job_kind="batch_write",
                commands=["interface GigabitEthernet0/0/1", "description x"],
                device_ids=[1, 2],
            )
            self.assertFalse(preview["risk"]["is_dangerous"])
            self.assertEqual(preview["estimated_minutes"], 1)
            self.assertEqual(preview["notes"], [])
        finally:
            db.close()

    def test_dangerous_commands_flag_and_note(self) -> None:
        db = self._session()
        try:
            preview = dry_run(
                db,
                job_kind="batch_write",
                commands=["reload"],
                device_ids=[1, 2],
            )
            self.assertTrue(preview["risk"]["is_dangerous"])
            self.assertEqual(preview["risk"]["matched_commands"], ["reload"])
            self.assertTrue(any("高危" in n for n in preview["notes"]))
        finally:
            db.close()

    def test_readonly_kind_skips_template_render(self) -> None:
        db = self._session()
        try:
            preview = dry_run(
                db,
                job_kind="batch_read",
                commands=["display version"],
                device_ids=[1],
            )
            self.assertEqual(preview["commands_by_device"], [])
            self.assertIn("只读", preview["notes"][0])
        finally:
            db.close()

    def test_large_fleet_warns_to_split(self) -> None:
        db = self._session()
        try:
            preview = dry_run(
                db,
                job_kind="batch_write",
                commands=["interface X", "shutdown"],
                device_ids=list(range(80)),
            )
            self.assertTrue(any("拆分" in n for n in preview["notes"]))
        finally:
            db.close()

    def test_template_id_missing_raises(self) -> None:
        db = self._session()
        try:
            with self.assertRaises(ValueError):
                dry_run(
                    db,
                    job_kind="batch_write",
                    template_id=999,
                    params={},
                    device_ids=[1],
                )
        finally:
            db.close()


class CountDiffLinesTest(unittest.TestCase):
    def test_identical_text_is_zero(self) -> None:
        adds, dels = _count_diff_lines("a\nb\nc", "a\nb\nc")
        self.assertEqual((adds, dels), (0, 0))

    def test_pure_addition(self) -> None:
        adds, dels = _count_diff_lines("a", "a\nb\nc")
        self.assertEqual(dels, 0)
        self.assertEqual(adds, 2)

    def test_pure_deletion(self) -> None:
        adds, dels = _count_diff_lines("a\nb\nc", "a")
        self.assertEqual(dels, 2)
        self.assertEqual(adds, 0)


class RollbackPlanTest(unittest.TestCase):
    def _session_with_job(self, *, before_ids: list[int], after_ids: list[int]):
        engine, _path = _make_engine()
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        job = Job(
            name="t", kind="batch_write", status="draft",
            target_device_ids=[1, 2, 3], initiated_by="tester",
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        for did in before_ids:
            tmp = tempfile.NamedTemporaryFile(
                suffix=f"_{did}_before.cfg", delete=False
            )
            tmp.write(b"before-config")
            tmp.close()
            db.add(BackupSnapshot(
                job_id=job.id, device_id=did, trigger="before",
                content_hash=f"hash_b_{did}", storage_path=tmp.name,
                size_bytes=14, note="job#x",
            ))
        for did in after_ids:
            tmp = tempfile.NamedTemporaryFile(
                suffix=f"_{did}_after.cfg", delete=False
            )
            tmp.write(b"after-config")
            tmp.close()
            db.add(BackupSnapshot(
                job_id=job.id, device_id=did, trigger="after",
                content_hash=f"hash_a_{did}", storage_path=tmp.name,
                size_bytes=12, note="job#x",
            ))
        db.commit()
        db.refresh(job)
        return db, job

    def test_full_coverage_is_auto_restore(self) -> None:
        db, job = self._session_with_job(before_ids=[1, 2, 3], after_ids=[1, 2, 3])
        try:
            plan = build_rollback_plan(db, job_id=job.id)
            self.assertEqual(plan["strategy"], "auto_backup_restore")
            self.assertEqual(plan["missing_devices"], [])
            self.assertEqual(len(plan["steps"]), 3)
        finally:
            db.close()

    def test_partial_coverage_is_config_patch(self) -> None:
        db, job = self._session_with_job(before_ids=[1, 2], after_ids=[1, 2])
        job.target_device_ids = [1, 2, 3]
        db.commit()
        try:
            plan = build_rollback_plan(db, job_id=job.id)
            self.assertEqual(plan["strategy"], "config_patch")
            self.assertEqual(plan["missing_devices"], [3])
        finally:
            db.close()

    def test_no_baseline_is_manual(self) -> None:
        db, job = self._session_with_job(before_ids=[], after_ids=[1, 2, 3])
        try:
            plan = build_rollback_plan(db, job_id=job.id)
            self.assertEqual(plan["strategy"], "manual")
            self.assertEqual(plan["steps"], [])
        finally:
            db.close()


class AIComparisonTest(unittest.TestCase):
    def test_identical_hashes_are_as_expected(self) -> None:
        engine, _path = _make_engine()
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        job = Job(
            name="t", kind="batch_write", status="draft",
            target_device_ids=[1], initiated_by="tester",
        )
        db.add(job); db.commit(); db.refresh(job)
        for did, trig in ((1, "before"), (1, "after")):
            tmp = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False)
            tmp.write(b"same")
            tmp.close()
            db.add(BackupSnapshot(
                job_id=job.id, device_id=did, trigger=trig,
                content_hash="same-hash", storage_path=tmp.name,
                size_bytes=4, note="t",
            ))
        db.commit(); db.refresh(job)
        try:
            comp = compute_ai_comparison(db, job_id=job.id)
            self.assertEqual(comp["verdict"], "as_expected")
            self.assertEqual(comp["deltas"][0]["verdict"], "as_expected")
        finally:
            db.close()

    def test_changed_hashes_minor_vs_major(self) -> None:
        engine, _path = _make_engine()
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        job = Job(
            name="t", kind="batch_write", status="draft",
            target_device_ids=[1, 2], initiated_by="tester",
        )
        db.add(job); db.commit(); db.refresh(job)

        # device 1: tiny change (minor)
        b1 = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False); b1.write(b"aaa"); b1.close()
        a1 = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False); a1.write(b"aab"); a1.close()
        db.add(BackupSnapshot(job_id=job.id, device_id=1, trigger="before",
                              content_hash="h1b", storage_path=b1.name, size_bytes=3))
        db.add(BackupSnapshot(job_id=job.id, device_id=1, trigger="after",
                              content_hash="h1a", storage_path=a1.name, size_bytes=3))

        # device 2: massive change (>50 lines, major)
        b2 = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False); b2.close()
        a2 = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False); a2.close()
        Path(b2.name).write_text("\n".join(f"line {i}" for i in range(5)))
        Path(a2.name).write_text("\n".join(f"new {i}" for i in range(80)))
        db.add(BackupSnapshot(job_id=job.id, device_id=2, trigger="before",
                              content_hash="h2b", storage_path=b2.name, size_bytes=50))
        db.add(BackupSnapshot(job_id=job.id, device_id=2, trigger="after",
                              content_hash="h2a", storage_path=a2.name, size_bytes=400))
        db.commit(); db.refresh(job)
        try:
            comp = compute_ai_comparison(db, job_id=job.id, ai_summary="ok")
            self.assertEqual(comp["verdict"], "major_diff")
            verdicts = sorted(d["verdict"] for d in comp["deltas"])
            self.assertIn("minor_diff", verdicts)
            self.assertIn("major_diff", verdicts)
            self.assertEqual(comp["ai_summary"], "ok")
        finally:
            db.close()

    def test_missing_after_marks_failed(self) -> None:
        engine, _path = _make_engine()
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        job = Job(
            name="t", kind="batch_write", status="draft",
            target_device_ids=[1], initiated_by="tester",
        )
        db.add(job); db.commit(); db.refresh(job)
        try:
            comp = compute_ai_comparison(db, job_id=job.id)
            self.assertEqual(comp["verdict"], "failed")
            self.assertTrue(any("after" in w for w in comp["warnings"]))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
