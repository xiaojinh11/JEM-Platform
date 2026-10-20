"""Registration and password-recovery endpoints (public, unauthenticated)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import account as account_svc

router = APIRouter()


class RegisterRequestBody(BaseModel):
    email: str = Field(..., max_length=255)


class VerifyAndRegisterBody(BaseModel):
    email: str = Field(..., max_length=255)
    code: str = Field(..., min_length=4, max_length=16)
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(None, max_length=128)


class ResetRequestBody(BaseModel):
    email: str = Field(..., max_length=255)


class ResetConfirmBody(BaseModel):
    token: str = Field(..., min_length=20)
    new_password: str = Field(..., min_length=8, max_length=128)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/register/request-code")
def register_request_code(body: RegisterRequestBody, request: Request, db: Session = Depends(get_db)):
    """Email a 6-digit verification code to the user."""
    ok, message, smtp_sent = account_svc.request_registration(db, body.email)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "smtp_sent": smtp_sent}


@router.post("/register/verify")
def register_verify(body: VerifyAndRegisterBody, request: Request, db: Session = Depends(get_db)):
    """Verify the code and create the user account."""
    ok, message, _user = account_svc.verify_and_register(
        db,
        email=body.email,
        code=body.code,
        username=body.username,
        password=body.password,
        full_name=body.full_name,
        request_ip=_client_ip(request),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}


@router.post("/password-reset/request")
def password_reset_request(body: ResetRequestBody, request: Request, db: Session = Depends(get_db)):
    """Email a password-reset link."""
    ok, message = account_svc.request_password_reset(db, body.email, request_ip=_client_ip(request))
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}


@router.post("/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmBody, request: Request, db: Session = Depends(get_db)):
    """Validate the reset token and apply the new password."""
    ok, message = account_svc.confirm_password_reset(
        db, token=body.token, new_password=body.new_password, request_ip=_client_ip(request)
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message}


@router.get("/password-reset/validate")
def password_reset_validate(token: str, db: Session = Depends(get_db)):
    """Check whether the reset token is still usable. Used by the frontend
    before showing the new-password form."""
    import hashlib
    from datetime import datetime, timezone
    from app.models.account import PasswordResetToken

    if not token or len(token) < 20:
        raise HTTPException(status_code=400, detail="无效的重置链接")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.query(PasswordResetToken).filter_by(token_hash=token_hash).first()
    if not row or row.used_at is not None:
        raise HTTPException(status_code=400, detail="重置链接无效")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="重置链接已过期")
    return {"ok": True, "expires_at": row.expires_at.isoformat()}
