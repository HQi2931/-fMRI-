"""Bind one-time execution confirmations to an exact WorkTask step.

Revision ID: 0036_task_step_run_authorizations
Revises: 0035_preprocessing_goal_admission
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_task_step_run_authorizations"
down_revision: str | None = "0035_preprocessing_goal_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("confirmation_challenges", sa.Column("task_id", sa.String(100), nullable=True))
    op.add_column("confirmation_challenges", sa.Column("step_id", sa.String(100), nullable=True))
    op.add_column("confirmation_challenges", sa.Column("task_version", sa.Integer(), nullable=True))
    op.add_column("confirmation_challenges", sa.Column("step_version", sa.Integer(), nullable=True))
    op.add_column("confirmation_challenges", sa.Column("plan_version", sa.Integer(), nullable=True))
    op.add_column(
        "confirmation_challenges", sa.Column("execution_backend", sa.String(20), nullable=True)
    )
    op.add_column(
        "confirmation_challenges", sa.Column("workspace_mode", sa.String(20), nullable=True)
    )
    op.create_index(
        "ix_confirmation_challenges_task_id",
        "confirmation_challenges",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_confirmation_challenges_task_id", table_name="confirmation_challenges")
    op.drop_column("confirmation_challenges", "workspace_mode")
    op.drop_column("confirmation_challenges", "execution_backend")
    op.drop_column("confirmation_challenges", "plan_version")
    op.drop_column("confirmation_challenges", "step_version")
    op.drop_column("confirmation_challenges", "task_version")
    op.drop_column("confirmation_challenges", "step_id")
    op.drop_column("confirmation_challenges", "task_id")
