"""add document index status columns

Revision ID: 002_add_document_index_status
Revises: 001
Create Date: 2026-09-19

Adds two columns to ``documents`` so the upload pipeline can mark each row
as ``pending`` / ``ready`` / ``failed`` while the Celery worker parses the
uploaded file. Backfills existing rows to ``ready`` because their Whoosh
index entries were created synchronously by the previous code path.

The ``index_status`` index lets the dashboard query "how many uploads are
still parsing?" without a full table scan.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_add_document_index_status"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "documents"
_INDEX_NAME = "ix_documents_index_status"


def upgrade() -> None:
    # 1. Add the columns. ``index_status`` defaults to "pending" for new
    #    uploads (which the Celery worker will flip to "ready").
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "index_status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            )
        )
        batch.add_column(
            sa.Column(
                "index_error",
                sa.String(length=500),
                nullable=True,
            )
        )

    # 2. Backfill. Existing rows were indexed synchronously, so they are
    #    effectively "ready". Doing this before the index is created keeps
    #    the index build cheap (no rewrite during the backfill).
    op.execute(
        f"UPDATE {_TABLE} SET index_status = 'ready' WHERE index_status = 'pending'"
    )

    # 3. Index. The model declares ``index=True`` for the column, so
    #    ``create_index`` mirrors what a fresh ``Base.metadata.create_all``
    #    would produce.
    op.create_index(_INDEX_NAME, _TABLE, ["index_status"])


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("index_error")
        batch.drop_column("index_status")
