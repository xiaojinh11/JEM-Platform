"""Document schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DocumentMeta(BaseModel):
    title: str
    vendor: Optional[str] = None
    category: Optional[str] = None
    version_tag: Optional[str] = None
    source: Optional[str] = None
    # Optional: explicit uploader. Server falls back to the JWT user.
    created_by_user_id: Optional[int] = Field(
        None, description="FK to users.id. Defaults to the caller's user id."
    )


class DocumentOut(BaseModel):
    id: int
    title: str
    vendor: Optional[str] = None
    category: Optional[str] = None
    version_tag: Optional[str] = None
    filename: str
    size_bytes: int
    content_hash: str
    summary: Optional[str] = None
    source: Optional[str] = None
    uploaded_by: str
    created_at: datetime
    # Exposed so the UI can show "解析中…" / "解析失败" badges without a
    # separate round trip. New columns added in C9 — all existing clients
    # tolerate missing keys via ``model_config``.
    index_status: Optional[str] = "pending"
    index_error: Optional[str] = None

    model_config = {"from_attributes": True}


class DocumentSearchHit(BaseModel):
    id: int
    title: str
    vendor: Optional[str] = None
    category: Optional[str] = None
    snippet: str
    score: float


class DocumentSearchResponse(BaseModel):
    query: str
    total: int
    hits: list[DocumentSearchHit]


class DocumentPage(BaseModel):
    """Paginated envelope for document list responses.

    The previous contract returned a bare ``list[DocumentOut]``, which
    forced the frontend to fetch every row before rendering anything and
    gave it no signal that more results exist. Now the caller passes
    ``limit``/``offset`` and gets ``total`` for paging UI plus the slice.
    """

    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class OfflineHealthSnapshot(BaseModel):
    """Vendor package integrity snapshot.

    Surfaced both in the offline-manual viewer (so the operator can tell
    at a glance whether the package is broken) and on the document list
    so the portal can flag bad packages before anyone opens them.
    """

    score: int = Field(ge=0, le=100, description="0-100, higher is healthier")
    grade: str = Field(description="ok | minor | warn | broken")
    total_members: int
    html_members: int
    css_members: int
    references_total: int
    references_missing: int
    references_case_only: int = 0
    references_by_kind: dict[str, int] = Field(default_factory=dict)
    missing_by_kind: dict[str, int] = Field(default_factory=dict)
    missing_targets: list[tuple[str, int]] = Field(default_factory=list)
    case_only_targets: list[tuple[str, int]] = Field(default_factory=list)
    top_missing_sources: list[tuple[str, int]] = Field(default_factory=list)


class DocumentDetail(DocumentOut):
    storage_path: Optional[str] = None
    content: Optional[str] = None
    is_binary: bool = False
    encoding: Optional[str] = None  # 'utf-8' | 'gbk' | 'gb18030' | 'latin-1' | 'binary'
    # Populated only when the document is a vendor offline manual
    # (``.hdx``) so the viewer can surface package defects.
    offline_health: Optional[OfflineHealthSnapshot] = None


class DocumentUpdate(BaseModel):
    """PATCH body for updating document metadata."""
    title: Optional[str] = Field(default=None, min_length=1, max_length=256)
    vendor: Optional[str] = Field(default=None, max_length=32)
    category: Optional[str] = Field(default=None, description="分类，固定为 产品手册，忽略传入的其他值")
    version_tag: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=128)
