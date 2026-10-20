"""Credential schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CredentialIn(BaseModel):
    name: str = Field(..., max_length=128)
    description: Optional[str] = Field(None, max_length=255)
    username: str = Field(..., max_length=64)
    password: Optional[str] = Field(None, max_length=16384)  # Allow large SSH keys up to RSA 8192-bit
    ssh_key: Optional[str] = Field(None, max_length=16384)  # SSH private key content
    enable_password: Optional[str] = Field(None, max_length=1024)
    secret_kind: str = Field(default="password", description="password | ssh_key")
    # Optional: when set, the credential is "owned" by a single device.
    # Nullable because shared credentials (e.g. service accounts) are common.
    device_id: Optional[int] = Field(None, description="Bind credential to a specific device (FK)")


class CredentialOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    username: str
    secret_kind: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
