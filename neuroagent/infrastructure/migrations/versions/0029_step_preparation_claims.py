"""Persist exact READY TaskStep preparation targets and fenced claims.

Revision ID: 0029_step_preparation_claims
Revises: 0028_composite_goal_links
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_step_preparation_claims"
down_revision: str | None = "0028_composite_goal_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "work_task_step_preparation_targets",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.String(100), nullable=False),
        sa.Column("step_kind", sa.String(100), nullable=False),
        sa.Column("step_version", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("task_id", "step_id", name="uq_step_preparation_target_step"),
        sa.CheckConstraint("task_version >= 1", name="ck_step_preparation_target_task_version"),
        sa.CheckConstraint("step_version >= 1", name="ck_step_preparation_target_step_version"),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name="ck_step_preparation_target_fingerprint_length",
        ),
    )
    op.create_index(
        "ix_step_preparation_target_schedule",
        "work_task_step_preparation_targets",
        ["step_kind", "task_version", "task_id"],
    )
    op.create_table(
        "work_task_step_preparation_claims",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("available_at", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_step_preparation_claim_shape",
        ),
        sa.CheckConstraint("available_at >= 0", name="ck_step_preparation_claim_available_at"),
    )
    op.create_index(
        "ix_step_preparation_claim_schedule",
        "work_task_step_preparation_claims",
        ["available_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_step_preparation_claim_schedule",
        table_name="work_task_step_preparation_claims",
    )
    op.drop_table("work_task_step_preparation_claims")
    op.drop_index(
        "ix_step_preparation_target_schedule",
        table_name="work_task_step_preparation_targets",
    )
    op.drop_table("work_task_step_preparation_targets")
