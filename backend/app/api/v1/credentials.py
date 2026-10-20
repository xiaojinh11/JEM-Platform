"""Credential CRUD."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, require_operator, require_operator
from app.db.session import get_db
from app.models.credential import Credential
from app.models.user import User
from app.schemas.credential import CredentialIn, CredentialOut
from app.services.credentials import create_credential, delete_credential, get, list_credentials, reveal_secret
from app.services.audit import record as audit_record

router = APIRouter()


@router.get("", response_model=list[CredentialOut])
def list_creds(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return list_credentials(db)


@router.get("/{cred_id}", response_model=CredentialOut)
def get_cred(cred_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    cred = get(db, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    return cred


@router.post("", response_model=CredentialOut, status_code=201)
def create_cred(payload: CredentialIn, _user=Depends(require_operator), db: Session = Depends(get_db)):
    cred = create_credential(db, payload)
    audit_record(db, actor=_user.username, action="credential.create", target_kind="credential", target_id=str(cred.id), detail=cred.name)
    db.commit()
    return cred


@router.delete("/{cred_id}", status_code=204)
def delete_cred(cred_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    cred = get(db, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    delete_credential(db, cred)
    return None


@router.get("/{cred_id}/reveal")
def reveal(cred_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    """Admin-only: decrypt and return plaintext secret."""
    cred = get(db, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    return reveal_secret(cred)
