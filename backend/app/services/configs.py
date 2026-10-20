"""Config archive service (revisions + diff)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import diff_match_patch
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.models.config import ConfigArchive, ConfigRevision
from app.schemas.config import ConfigImportOut
from app.utils.helpers import ensure_dir, safe_join


def _hash_text(text: str) -> tuple[str, int]:
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    return h.hexdigest(), len(text.encode("utf-8"))


def _revision_path(archive_id: int, revision_no: int) -> Path:
    root = ensure_dir(settings.storage_configs_dir / f"archive_{archive_id}")
    return root / f"r{revision_no:06d}.cfg"


def get_or_create_archive(db: Session, device_id: int) -> ConfigArchive:
    arc = db.query(ConfigArchive).filter_by(device_id=device_id).one_or_none()
    if arc is None:
        arc = ConfigArchive(device_id=device_id)
        db.add(arc)
        db.flush()
    return arc


def list_revisions(db: Session, device_id: int) -> list[ConfigRevision]:
    arc = db.query(ConfigArchive).filter_by(device_id=device_id).one_or_none()
    if arc is None:
        return []
    return (
        db.query(ConfigRevision)
        .filter_by(archive_id=arc.id)
        .order_by(ConfigRevision.revision_no.desc())
        .all()
    )


def get_revision(db: Session, revision_id: int) -> Optional[ConfigRevision]:
    return db.get(ConfigRevision, revision_id)


def read_revision_text(rev: ConfigRevision) -> str:
    p = Path(rev.storage_path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def import_text(
    db: Session,
    *,
    device_id: int,
    content: str,
    author: str,
    source: str = "import",
    note: Optional[str] = None,
) -> ConfigImportOut:
    arc = get_or_create_archive(db, device_id)
    last_no = (
        db.query(ConfigRevision)
        .filter_by(archive_id=arc.id)
        .order_by(ConfigRevision.revision_no.desc())
        .first()
    )
    next_no = (last_no.revision_no + 1) if last_no else 1
    content_hash, size = _hash_text(content)
    storage_path = _revision_path(arc.id, next_no)
    storage_path.write_text(content, encoding="utf-8")
    rev = ConfigRevision(
        archive_id=arc.id,
        revision_no=next_no,
        source=source,
        author=author,
        note=note,
        content_hash=content_hash,
        size_bytes=size,
        storage_path=str(storage_path),
    )
    db.add(rev)
    db.flush()
    arc.latest_revision_id = rev.id
    db.commit()
    db.refresh(rev)
    return ConfigImportOut(revision_id=rev.id, revision_no=rev.revision_no, content_hash=rev.content_hash)


def diff_revisions(db: Session, from_id: int, to_id: int) -> dict:
    a = get_revision(db, from_id)
    b = get_revision(db, to_id)
    if not a or not b:
        raise ValueError("revision not found")
    left = read_revision_text(a)
    right = read_revision_text(b)
    dmp = diff_match_patch.diff_match_patch()
    dmp.Diff_Timeout = 5
    diffs = dmp.diff_main(left, right)
    dmp.diff_cleanupSemantic(diffs)
    html = dmp.diff_prettyHtml(diffs)
    adds = sum(1 for d, _ in diffs if d > 0)
    dels = sum(1 for d, _ in diffs if d < 0)
    return {
        "from_revision_id": from_id,
        "to_revision_id": to_id,
        "diff_html": html,
        "additions": adds,
        "deletions": dels,
    }


def store_snapshot(*, device_id: int, content: str, trigger: str, note: str | None, job_id: Optional[int] = None):
    """Write a snapshot to the backups dir and return metadata (no DB commit)."""
    h, size = _hash_text(content)
    folder = ensure_dir(settings.storage_backups_dir / f"device_{device_id}")
    p = safe_join(folder, f"{int.from_bytes(h.encode()[:4], 'big')}_{int(time.time())}.cfg")
    p.write_text(content, encoding="utf-8")
    return {
        "device_id": device_id,
        "content_hash": h,
        "storage_path": str(p),
        "size_bytes": size,
        "trigger": trigger,
        "note": note,
        "job_id": job_id,
    }


import time  # noqa: E402  (used by store_snapshot)
