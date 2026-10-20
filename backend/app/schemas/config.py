"""Configuration archive schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ConfigRevisionOut(BaseModel):
    id: int
    revision_no: int
    source: str
    author: str
    note: Optional[str] = None
    content_hash: str
    size_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ConfigImportIn(BaseModel):
    device_id: int
    content: str = Field(..., description="Configuration text (UTF-8)")
    author: str
    source: str = Field(default="import")
    note: Optional[str] = None


class ConfigImportOut(BaseModel):
    revision_id: int
    revision_no: int
    content_hash: str


class ConfigDiffRequest(BaseModel):
    from_revision_id: int
    to_revision_id: int


class ConfigDiffResponse(BaseModel):
    from_revision_id: int
    to_revision_id: int
    diff_html: str
    additions: int
    deletions: int
