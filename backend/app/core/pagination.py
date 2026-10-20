"""Shared pagination helpers.

Centralizes the upper-bound enforcement so every list endpoint
adopts the same ``?page=&per_page=`` contract and we can't accidentally
introduce an unbounded query through a new endpoint.

Why an upper bound:
    Hard-coded ``.limit(100)`` is fine until someone adds ``?limit=``
    to satisfy a frontend request — at which point a hostile caller
    passes ``?limit=99999999`` and we OOM the worker.  This helper
    clamps the requested size against ``MAX_PER_PAGE`` *before* the
    query runs, regardless of how many other endpoints forget to
    clamp.
"""
from __future__ import annotations

from typing import TypeVar

from fastapi import Query
from sqlalchemy.orm import Query as SAQuery

T = TypeVar("T")

MAX_PER_PAGE = 200


def pagination_params(
    page: int = Query(1, ge=1, description="1-indexed page number"),
    per_page: int = Query(
        50,
        ge=1,
        description=f"Items per page (max {MAX_PER_PAGE})",
    ),
) -> tuple[int, int]:
    """Resolve page / per_page with a hard ceiling.

    Returns ``(page, per_page)`` ready for use with
    ``apply_pagination(query, page, per_page)``.
    """
    return page, min(per_page, MAX_PER_PAGE)


def apply_pagination(query: SAQuery[T], page: int, per_page: int) -> SAQuery[T]:
    """Apply offset/limit to a SQLAlchemy query.

    ``per_page`` should already be clamped — use ``pagination_params``
    at the route boundary so we never have to trust raw user input
    here.
    """
    return query.offset((page - 1) * per_page).limit(per_page)


def paginated_payload(items: list[T], page: int, per_page: int, total: int) -> dict:
    """Standard envelope so the frontend has a uniform shape.

    Includes ``total_pages`` so the UI can render a page-count without
    recomputing it from ``total / per_page`` (which is wrong on
    boundary cases — see the docs).
    """
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
    }
