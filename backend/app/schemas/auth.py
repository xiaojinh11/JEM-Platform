"""Auth-related schemas."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# P0 Security: shared password strength rule. Used by both initial
# user creation and password change to prevent weak passwords from
# ever landing in the database (regardless of who creates the user).
_PASSWORD_RULE = re.compile(r"[A-Z]")  # also requires a digit, see below
_PASSWORD_MIN_LEN = 8


def _validate_password_strength(value: str) -> str:
    """Enforce: ≥8 chars, at least one uppercase letter and one digit.

    Returns the value unchanged on success so callers can chain other
    validators (Field min_length already enforces the length floor —
    this validator adds the *character-class* checks that pydantic's
    Field can't express declaratively).
    """
    if len(value) < _PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少 {_PASSWORD_MIN_LEN} 位")
    if not _PASSWORD_RULE.search(value):
        raise ValueError("密码必须包含至少一个大写字母")
    if not re.search(r"\d", value):
        raise ValueError("密码必须包含至少一个数字")
    return value


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int  # access token lifetime, in seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8)
    full_name: Optional[str] = None
    role: str = "operator"

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)
