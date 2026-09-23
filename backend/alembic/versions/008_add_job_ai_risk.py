"""Add AI-driven risk assessment / rollback plan columns to jobs

Revision ID: 008_add_job_ai_risk
Revises: 20260923_133126
Create Date: 2026-09-23

Background
----------
Phase D of the AI-assistant rollout makes the *change-intelligence* path
AI-first. Before any high-risk job runs we want a structured risk
assessment, a rollback plan, and — after the job completes — a
diff between expected and actual outcomes.

New nullable JSON columns (kept nullable so existing jobs keep working):

- ``risk_assessment``: ``{level, score, factors, affected_devices,
  dry_run_summary, model, tokens, generated_at}``
- ``risk_assessed_at``: when the AI was last consulted
- ``risk_assessment_error``: when the AI call failed
- ``rollback_plan``: ``{strategy, steps, estimated_minutes,
  dependencies, generated_at}``
- ``ai_comparison``: ``{expected, actual, deltas, warnings, verdict,
  generated_at}`` populated after the job finishes
- ``ai_comparison_at``: timestamp of the comparison

This migration is a no-op on a fresh DB that already has the columns
(SQLite stores JSON as TEXT). We still run it for upgrade parity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_add_job_ai_risk"
down_revision: Union[str, None] = "20260923_133126"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("risk_assessment", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("risk_assessed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("risk_assessment_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("rollback_plan", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("ai_comparison", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("ai_comparison_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("ai_comparison_at")
        batch.drop_column("ai_comparison")
        batch.drop_column("rollback_plan")
        batch.drop_column("risk_assessment_error")
        batch.drop_column("risk_assessed_at")
        batch.drop_column("risk_assessment")
