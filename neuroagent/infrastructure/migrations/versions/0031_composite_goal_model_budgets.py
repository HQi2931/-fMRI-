"""Persist shared model-call budgets for composite goals.

Revision ID: 0031_composite_goal_model_budgets
Revises: 0030_composite_goal_source_fanout
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0031_composite_goal_model_budgets"
down_revision: str | None = "0030_composite_goal_source_fanout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "composite_goal_model_budgets",
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("max_elapsed_seconds", sa.Integer(), nullable=False),
        sa.Column("used_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_links.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_model_budget_goal",
        ),
        sa.PrimaryKeyConstraint("intent_task_id"),
        sa.CheckConstraint("max_steps >= 1", name="ck_composite_goal_budget_max_steps"),
        sa.CheckConstraint("max_model_calls >= 0", name="ck_composite_goal_budget_max_calls"),
        sa.CheckConstraint("max_tokens >= 0", name="ck_composite_goal_budget_max_tokens"),
        sa.CheckConstraint("max_elapsed_seconds >= 1", name="ck_composite_goal_budget_max_elapsed"),
        sa.CheckConstraint("used_steps >= 0", name="ck_composite_goal_budget_used_steps"),
        sa.CheckConstraint("used_model_calls >= 0", name="ck_composite_goal_budget_used_calls"),
        sa.CheckConstraint("used_tokens >= 0", name="ck_composite_goal_budget_used_tokens"),
        sa.CheckConstraint("elapsed_seconds >= 0", name="ck_composite_goal_budget_elapsed"),
    )
    op.create_table(
        "composite_goal_model_call_reservations",
        sa.Column("reservation_id", sa.String(100), nullable=False),
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("call_id", sa.String(100), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_tokens", sa.Integer(), nullable=True),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_model_budgets.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_model_reservation_budget",
        ),
        sa.PrimaryKeyConstraint("intent_task_id", "reservation_id"),
        sa.UniqueConstraint(
            "intent_task_id", "call_id", name="uq_composite_goal_model_reservation_call"
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'settled', 'exhausted')",
            name="ck_composite_goal_model_reservation_state",
        ),
        sa.CheckConstraint(
            "estimated_tokens >= 0", name="ck_composite_goal_model_reservation_estimate"
        ),
        sa.CheckConstraint(
            "reserved_tokens >= 0", name="ck_composite_goal_model_reservation_reserved"
        ),
        sa.CheckConstraint(
            "actual_tokens IS NULL OR actual_tokens >= 0",
            name="ck_composite_goal_model_reservation_actual",
        ),
        sa.CheckConstraint(
            "elapsed_seconds >= 0", name="ck_composite_goal_model_reservation_elapsed"
        ),
    )
    op.create_index(
        "ix_composite_goal_model_reservation_goal",
        "composite_goal_model_call_reservations",
        ["intent_task_id", "state"],
    )
    _backfill_budgets_from_preprocessing_tasks(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_composite_goal_model_reservation_goal",
        table_name="composite_goal_model_call_reservations",
    )
    op.drop_table("composite_goal_model_call_reservations")
    op.drop_table("composite_goal_model_budgets")


def _backfill_budgets_from_preprocessing_tasks(connection: Any) -> None:
    """Freeze each existing root's limits from its exact preprocessing task."""

    rows = connection.execute(
        sa.text(
            "SELECT goal.intent_task_id, task.budget_json "
            "FROM composite_goal_links AS goal "
            "JOIN work_tasks AS task ON task.task_id=goal.preprocessing_task_id "
            "ORDER BY goal.intent_task_id"
        )
    ).mappings()
    for row in rows:
        intent_task_id = str(row["intent_task_id"])
        try:
            raw_budget = json.loads(row["budget_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot backfill model budget for composite goal {intent_task_id}: "
                "preprocessing task budget is invalid."
            ) from exc
        if not isinstance(raw_budget, dict):
            raise RuntimeError(
                f"Cannot backfill model budget for composite goal {intent_task_id}: "
                "preprocessing task budget is not an object."
            )
        caps = {
            "max_steps": raw_budget.get("max_steps"),
            "max_model_calls": raw_budget.get("max_model_calls"),
            "max_tokens": raw_budget.get("max_tokens"),
            "max_elapsed_seconds": raw_budget.get("max_elapsed_seconds"),
        }
        lower_bounds = {
            "max_steps": 1,
            "max_model_calls": 0,
            "max_tokens": 0,
            "max_elapsed_seconds": 1,
        }
        upper_bounds = {
            "max_steps": 1000,
            "max_model_calls": 1000,
            "max_tokens": 10_000_000,
            "max_elapsed_seconds": 31_536_000,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not lower_bounds[name] <= value <= upper_bounds[name]
            for name, value in caps.items()
        ):
            raise RuntimeError(
                f"Cannot backfill model budget for composite goal {intent_task_id}: "
                "preprocessing task budget limits are invalid."
            )
        connection.execute(
            sa.text(
                "INSERT INTO composite_goal_model_budgets "
                "(intent_task_id, max_steps, max_model_calls, max_tokens, max_elapsed_seconds, "
                "used_steps, used_model_calls, used_tokens, elapsed_seconds) "
                "VALUES (:intent_task_id, :max_steps, :max_model_calls, :max_tokens, "
                ":max_elapsed_seconds, 0, 0, 0, 0)"
            ),
            {"intent_task_id": intent_task_id, **caps},
        )
