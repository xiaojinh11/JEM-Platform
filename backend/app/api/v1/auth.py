"""Auth endpoints: login, whoami, user management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, touch_last_login
from app.core.config import settings
from app.core.ratelimit import login_ip_limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_revoked,
    revoke_token,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, PasswordChange, RefreshRequest, TokenResponse, UserCreate, UserOut
from app.services import account as account_svc
from app.services.audit import record as audit_record

router = APIRouter()


class BootstrapStatus(BaseModel):
    has_admin: bool


@router.get("/bootstrap-status", response_model=BootstrapStatus)
def bootstrap_status(db: Session = Depends(get_db)) -> BootstrapStatus:
    has_admin = db.query(User).filter_by(role="admin").first() is not None
    return BootstrapStatus(has_admin=has_admin)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    # P0 Security: per-IP throttle BEFORE the per-account lockout check.
    # Without this, an attacker can fan out usernames across one IP
    # without ever tripping the per-account lock — dictionary attack
    # on weak passwords survives.  Layered defenses.
    ip = request.client.host if request.client else "unknown"
    allowed, retry_after = login_ip_limiter.check(ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"IP 登录频率过高，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    # Lockout check — pre-empts brute-force attempts
    if account_svc.is_locked_out(db, payload.username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试次数过多，请稍后再试",
        )

    user = db.query(User).filter_by(username=payload.username).one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        account_svc.record_login_attempt(db, payload.username, ip, success=False)
        audit_record(
            db,
            actor=payload.username,
            action="login.failed",
            detail="invalid credentials",
            ip_address=ip,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账户待管理员审核或已被禁用",
        )
    access = create_access_token(subject=user.id, extra={"role": user.role})
    refresh = create_refresh_token(subject=user.id)
    touch_last_login(db, user)
    account_svc.record_login_attempt(db, payload.username, ip, success=True)
    audit_record(
        db,
        actor=user.username,
        action="login.success",
        ip_address=ip,
    )
    db.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh_access_token(payload: RefreshRequest, db: Session = Depends(get_db)):
    """Exchange a refresh JWT for a fresh access JWT.

    Why a separate token type:
      * Access tokens stay short (15min default) — stolen tokens have
        a narrow blast radius.
      * Refresh tokens last longer (7 days default) but can ONLY mint
        new access tokens via this endpoint.  They cannot be used to
        call protected APIs directly (guarded by ``typ=refresh``).

    Refresh tokens are rotated on every successful exchange and the
    *old* one is revoked — so a leaked refresh token is useful for at
    most one request, after which legitimate clients detect the
    rejection and re-authenticate.
    """
    try:
        claims = decode_token(payload.refresh_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    if claims.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="not a refresh token")
    if is_revoked(payload.refresh_token, db=db):
        raise HTTPException(status_code=401, detail="refresh token revoked")

    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="malformed refresh token")

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")

    # Rotate: revoke the presented refresh token, issue a fresh pair.
    revoke_token(
        db,
        payload.refresh_token,
        user_id=user.id,
        username=user.username,
        reason="refresh-rotation",
    )
    new_access = create_access_token(subject=user.id, extra={"role": user.role})
    new_refresh = create_refresh_token(subject=user.id)
    db.commit()
    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, request: Request, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    if db.query(User).filter_by(username=payload.username).first():
        raise HTTPException(status_code=409, detail="username already exists")
    user = User(
        username=payload.username,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit_record(
        db,
        actor=_admin.username,
        action="user.create",
        target_kind="user",
        target_id=str(user.id),
        detail=f"role={user.role}",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(_admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.username.asc()).all()


@router.post("/users/{user_id}/approve", response_model=UserOut)
def approve_user(user_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Enable a freshly-registered user (flips is_active=True)."""
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if target.is_active:
        return target
    target.is_active = True
    audit_record(
        db,
        actor=admin.username,
        action="user.approve",
        target_kind="user",
        target_id=str(target.id),
        detail=f"approved registration for {target.username}",
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(target)
    return target


@router.post("/change-password")
def change_password(payload: PasswordChange, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="old password mismatch")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    audit_record(
        db,
        actor=user.username,
        action="user.change_password",
        target_kind="user",
        target_id=str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"ok": True}


@router.post("/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    authorization: str | None = None,
):
    """Revoke the bearer token used to authenticate this request.

    The token hash is written to ``revoked_tokens``; subsequent calls
    with the same token — even before its ``exp`` — return ``401``.

    Changing a password does NOT auto-revoke other sessions here:
    callers (or an admin force-logout workflow) must call ``/logout``
    per device.  This matches the "logout this tab, not my whole
    account" UX expectation.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    revoke_token(
        db,
        token,
        user_id=user.id,
        username=user.username,
        reason="logout",
    )
    audit_record(
        db,
        actor=user.username,
        action="user.logout",
        target_kind="user",
        target_id=str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"ok": True, "message": "token revoked"}
