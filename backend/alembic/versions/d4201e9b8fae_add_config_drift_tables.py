"""no-op placeholder (kept for existing DB compatibility)

Revision ID: d4201e9b8fae
Revises: drift_tables_v1

No structural changes. This file is a placeholder migration retained so that
existing databases (stamped at ``d4201e9b8fae``) can resolve this revision ID.

Linear chain from this point (both stub and main paths converge):
  d4201e9b8fae (here, no-op) -> drift_tables_v1 -> ai_scripts_v1 -> 003 -> 004 -> 005 (head)

Both the stub path (existing DBs) and main path (fresh DBs) run drift_tables_v1,
so the IF NOT EXISTS guards ensure idempotent table creation on both paths.
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "d4201e9b8fae"
down_revision: Union[str, None] = "drift_tables_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op bridge. All schema is created by drift_tables_v1 (IF NOT EXISTS guards).
    pass


def downgrade() -> None:
    pass
