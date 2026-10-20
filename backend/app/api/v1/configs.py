"""Config archive + revision endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.user import User
from app.schemas.config import ConfigDiffRequest, ConfigDiffResponse, ConfigImportIn, ConfigImportOut, ConfigRevisionOut
from app.services.configs import diff_revisions, get_revision, import_text, list_revisions, read_revision_text

router = APIRouter()


@router.get("/device/{device_id}/revisions", response_model=list[ConfigRevisionOut])
def list_revs(device_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return list_revisions(db, device_id)


@router.get("/revisions/{rev_id}/content")
def read_content(rev_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    rev = get_revision(db, rev_id)
    if not rev:
        raise HTTPException(status_code=404, detail="revision not found")
    return {"id": rev.id, "content": read_revision_text(rev)}


@router.post("/import", response_model=ConfigImportOut)
def import_config(payload: ConfigImportIn, _user=Depends(require_operator), db: Session = Depends(get_db)):
    try:
        result = import_text(
            db,
            device_id=payload.device_id,
            content=payload.content,
            author=payload.author or _user.username,
            source=payload.source,
            note=payload.note,
        )
        db.commit()
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/diff", response_model=ConfigDiffResponse)
def diff(payload: ConfigDiffRequest, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    try:
        result = diff_revisions(db, payload.from_revision_id, payload.to_revision_id)
        return ConfigDiffResponse(**result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
