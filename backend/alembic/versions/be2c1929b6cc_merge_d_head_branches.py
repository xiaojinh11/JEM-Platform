"""merge D-head branches

Revision ID: be2c1929b6cc
Revises: 006_add_revoked_tokens, 008_add_job_ai_risk
Create Date: 2026-09-23 19:16:21.859010

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'be2c1929b6cc'
down_revision: Union[str, None] = ('006_add_revoked_tokens', '008_add_job_ai_risk')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
