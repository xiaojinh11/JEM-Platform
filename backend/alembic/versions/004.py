"""add credentials.device_id and documents.created_by_user_id

Revision ID: 004_add_creator_fks
Revises: 003_add_account_tables
Create Date: 2026-09-21

The graph visualisation needs real FK relationships to wire edges in.
Specifically:

* ``credentials.device_id`` — links a credential to a single primary
  device. Nullable because some credentials (e.g. shared service
  accounts) aren't tied to one box.
* ``documents.created_by_user_id`` — links a document to the user that
  uploaded it. Distinct from ``uploaded_by`` (a free-text username) so
  we can JOIN through the users table.

Both columns are nullable to keep existing rows valid. The graph sync
script will skip rows where the FK is NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_add_creator_fks"
# Starts from the mergepoint of the two parallel development branches.
down_revision: Union[str, tuple, None] = "003_add_account_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("credentials") as batch:
        batch.add_column(
            sa.Column("device_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_credentials_device_id_devices",
            "devices",
            ["device_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_credentials_device_id", ["device_id"])

    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column("created_by_user_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_documents_created_by_user_id_users",
            "users",
            ["created_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_documents_created_by_user_id", ["created_by_user_id"])


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("fk_documents_created_by_user_id_users", type_="foreignkey")
        batch.drop_index("ix_documents_created_by_user_id")
        batch.drop_column("created_by_user_id")
    with op.batch_alter_table("credentials") as batch:
        batch.drop_constraint("fk_credentials_device_id_devices", type_="foreignkey")
        batch.drop_index("ix_credentials_device_id")
        batch.drop_column("device_id")
