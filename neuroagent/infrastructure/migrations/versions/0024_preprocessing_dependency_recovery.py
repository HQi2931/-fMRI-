"""Index preprocessing WorkTasks waiting for exact target verification.

Revision ID: 0024_preprocessing_dependency_recovery
Revises: 0023_work_task_reconciliation_claims
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0024_preprocessing_dependency_recovery"
down_revision: str | None = "0023_work_task_reconciliation_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _matches_preprocessing_downstream(row: Any, connection: Any) -> bool:
    try:
        waiting = json.loads(row["waiting_condition_json"] or "null")
        authority = json.loads(row["authority_refs_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    run_id = authority.get("workflow_run_id")
    plan_id = authority.get("plan_revision_id")
    if (
        row["status"] != "waiting_dependency"
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(plan_id, str)
        or not plan_id
        or not isinstance(waiting, dict)
        or waiting.get("kind") != "dependency"
        or waiting.get("reference") != f"downstream:{run_id}"
    ):
        return False

    rows = connection.execute(
        sa.text("SELECT step_json FROM work_task_steps WHERE task_id = :task_id"),
        {"task_id": row["task_id"]},
    ).mappings()
    matching_steps = []
    for step_row in rows:
        try:
            step = json.loads(step_row["step_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        refs = step.get("authority_refs") if isinstance(step, dict) else None
        if isinstance(step, dict) and step.get("kind") == "preprocessing_run":
            matching_steps.append((step, refs))
    if len(matching_steps) != 1:
        return False
    step, refs = matching_steps[0]
    return (
        step.get("status") == "waiting"
        and isinstance(refs, dict)
        and refs.get("workflow_run_id") == run_id
        and refs.get("plan_revision_id") == plan_id
    )


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT task_id, status, waiting_condition_json, authority_refs_json "
            "FROM work_tasks WHERE status = 'waiting_dependency'"
        )
    ).mappings()
    targets: list[dict[str, str]] = []
    for row in rows:
        if _matches_preprocessing_downstream(row, connection):
            authority = json.loads(row["authority_refs_json"])
            targets.append(
                {
                    "task_id": row["task_id"],
                    "run_id": authority["workflow_run_id"],
                    "target_kind": "workflow_run",
                }
            )
    if targets:
        op.bulk_insert(
            sa.table(
                "work_task_reconciliation_targets",
                sa.column("task_id", sa.String(100)),
                sa.column("run_id", sa.String(100)),
                sa.column("target_kind", sa.String(32)),
            ),
            targets,
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT task.task_id, task.status, task.waiting_condition_json, "
            "task.authority_refs_json "
            "FROM work_task_reconciliation_targets AS target "
            "JOIN work_tasks AS task ON task.task_id = target.task_id "
            "WHERE target.target_kind = 'workflow_run' "
            "AND task.status = 'waiting_dependency'"
        )
    ).mappings()
    for row in rows:
        if _matches_preprocessing_downstream(row, connection):
            connection.execute(
                sa.text("DELETE FROM work_task_reconciliation_targets WHERE task_id = :task_id"),
                {"task_id": row["task_id"]},
            )
