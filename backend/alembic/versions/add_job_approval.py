"""add job approval columns

Revision ID: add_job_approval
Revises:
Create Date: 2026-09-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_job_approval'
# Branches from the initial schema. After the document-index-status
# migration (002) was added, this revision's ``down_revision`` was
# updated from ``None`` to ``002`` so the graph is a single linear
# chain. ``001`` would also be technically valid as a parent; ``002``
# is preferred because it preserves the original two-migration order
# of "first the schema, then app-specific columns".
down_revision = '002_add_document_index_status'
branch_labels = None
depends_on = None


def upgrade():
    # Add approval columns to jobs table
    op.add_column('jobs', sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('jobs', sa.Column('approved_by', sa.String(length=64), nullable=True))
    op.add_column('jobs', sa.Column('approved_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('jobs', 'approved_at')
    op.drop_column('jobs', 'approved_by')
    op.drop_column('jobs', 'requires_approval')
