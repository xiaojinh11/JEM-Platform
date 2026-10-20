"""Credential service."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.credential import Credential
from app.schemas.credential import CredentialIn


def create_credential(db: Session, data: CredentialIn) -> Credential:
    secret = data.ssh_key if data.secret_kind == "ssh_key" else data.password
    if not secret:
        raise ValueError("either password or ssh_key must be provided")
    enable_enc = encrypt_secret(data.enable_password) if data.enable_password else None
    cred = Credential(
        name=data.name,
        description=data.description,
        username=data.username,
        secret_kind=data.secret_kind,
        secret_enc=encrypt_secret(secret),
        enable_password_enc=enable_enc,
        device_id=data.device_id,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def list_credentials(db: Session) -> list[Credential]:
    return db.query(Credential).order_by(Credential.name.asc()).all()


def get(db: Session, credential_id: int) -> Optional[Credential]:
    return db.get(Credential, credential_id)


def reveal_secret(cred: Credential) -> dict:
    return {
        "id": cred.id,
        "name": cred.name,
        "username": cred.username,
        "secret_kind": cred.secret_kind,
        "password": None if cred.secret_kind == "ssh_key" else decrypt_secret(cred.secret_enc),
        "ssh_key": decrypt_secret(cred.secret_enc) if cred.secret_kind == "ssh_key" else None,
        "enable_password": decrypt_secret(cred.enable_password_enc) if cred.enable_password_enc else None,
    }


def delete_credential(db: Session, cred: Credential) -> None:
    db.delete(cred)
    db.commit()
