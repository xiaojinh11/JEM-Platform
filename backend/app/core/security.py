"""Security primitives: password hashing, JWT, Fernet credential encryption."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_fernet = Fernet(settings.fernet_key.encode())


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001 - bcrypt raises on malformed hash
        return False


def create_access_token(subject: str | int, extra: dict[str, Any] | None = None, expires_minutes: int | None = None) -> str:
    """Mint a short-lived access JWT.

    Each token carries a fresh ``jti`` (UUID4) and a ``typ=access`` tag.
    ``jti`` is what ``revoke_token()`` references, and ``typ`` lets us
    reject password-reset / offline-manual tokens at the standard auth
    boundary if anyone ever routes them through the wrong validator.

    Default lifetime is 15 minutes — short enough that a stolen token
    has a narrow blast radius.  Long-lived sessions are handled by the
    separate refresh token (see ``create_refresh_token``).
    """
    minutes = expires_minutes or settings.jwt_expire_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str | int) -> str:
    """Mint a long-lived refresh JWT.

    Refresh tokens are *only* accepted by ``/auth/refresh`` — they
    cannot be used to call the API directly (guarded by ``typ=refresh``
    in ``get_current_user``).  Default lifetime is 7 days; an admin can
    tighten this via ``NAW_REFRESH_TOKEN_EXPIRE_DAYS``.

    The token itself is opaque to the client; what matters is its
    ``jti`` (which is also written into ``revoked_tokens`` on logout or
    when it is rotated).
    """
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "jti": uuid.uuid4().hex,
        "typ": "refresh",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:  # noqa: BLE001
        raise ValueError("invalid token") from exc


def hash_token(token: str) -> str:
    """Stable hash used to look up tokens in the revocation table."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_revoked(token: str, db: Optional[Any] = None) -> bool:
    """Return True iff the token (or its hash) appears in ``revoked_tokens``.

    ``db`` is optional.  When it's ``None`` we open a short-lived
    session so callers that don't already hold one (e.g. the WebSocket
    auth helper, which previously returned ``None`` for invalid tokens
    and closed the socket) can still get a revocation check without
    inheriting the request-scoped DB dependency.
    """
    from app.db.session import SessionLocal
    from app.models.revoked_token import RevokedToken

    token_hash = hash_token(token)
    own_session = False
    try:
        if db is None:
            db = SessionLocal()
            own_session = True
        row = (
            db.query(RevokedToken)
            .filter(RevokedToken.token_hash == token_hash)
            .first()
        )
        return row is not None
    finally:
        if own_session:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def revoke_token(
    db: Any,
    token: str,
    *,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Add a token to the revocation list.

    Pulls ``exp`` straight out of the JWT so the row's ``expires_at``
    matches the JWT's actual lifetime — the entry can be pruned safely
    once ``expires_at`` is in the past.
    """
    from app.models.revoked_token import RevokedToken

    try:
        payload = decode_token(token)
    except ValueError:
        # Malformed token: nothing useful to revoke.
        return
    exp_ts = payload.get("exp")
    if isinstance(exp_ts, (int, float)):
        expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.add(
        RevokedToken(
            token_hash=hash_token(token),
            user_id=user_id or (int(payload["sub"]) if str(payload.get("sub", "")).isdigit() else None),
            username=username or str(payload.get("username") or "") or None,
            expires_at=expires_at,
            revoked_at=datetime.now(timezone.utc),
            reason=reason,
        )
    )
    db.flush()


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("invalid ciphertext") from exc
