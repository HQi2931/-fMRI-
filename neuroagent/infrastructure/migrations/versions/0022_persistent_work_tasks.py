"""Persist goal-oriented Work tasks, steps, event cursors and dedupe receipts.

Revision ID: 0022_persistent_work_tasks
Revises: 0021_work_task_bindings
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_persistent_work_tasks"
down_revision: str | None = "0021_work_task_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_tasks",
        sa.Column("task_id", sa.String(100), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("goal_json", sa.Text(), nullable=False),
        sa.Column("waiting_condition_json", sa.Text(), nullable=True),
        sa.Column("authority_refs_json", sa.Text(), nullable=False),
        sa.Column("recovery_steps_json", sa.Text(), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_work_tasks_version_positive"),
    )
    op.create_index("ix_work_tasks_status", "work_tasks", ["status"])
    op.create_table(
        "work_task_steps",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("step_id", sa.String(100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("step_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "step_id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_work_task_steps_sequence"),
        sa.CheckConstraint("sequence >= 0", name="ck_work_task_steps_sequence_nonnegative"),
        sa.CheckConstraint("version >= 1", name="ck_work_task_steps_version_positive"),
    )
    op.create_table(
        "work_task_event_cursors",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("stream_key", sa.String(300), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "stream_key"),
        sa.CheckConstraint("event_id >= 0", name="ck_work_task_cursor_nonnegative"),
    )
    op.create_table(
        "work_task_event_receipts",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("dedupe_key", sa.String(300), nullable=False),
        sa.Column("stream_key", sa.String(300), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "dedupe_key"),
        sa.UniqueConstraint(
            "task_id", "stream_key", "event_id", name="uq_work_task_event_identity"
        ),
        sa.CheckConstraint("event_id >= 1", name="ck_work_task_receipt_event_positive"),
    )


def downgrade() -> None:
    op.drop_table("work_task_event_receipts")
    op.drop_table("work_task_event_cursors")
    op.drop_table("work_task_steps")
    op.drop_index("ix_work_tasks_status", table_name="work_tasks")
    op.drop_table("work_tasks")
