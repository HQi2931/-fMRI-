"""Add indexed wake targets and fenced leases for background WorkTask recovery.

Revision ID: 0023_work_task_reconciliation_claims
Revises: 0022_persistent_work_tasks
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0023_work_task_reconciliation_claims"
down_revision: str | None = "0022_persistent_work_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_task_reconciliation_targets",
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["work_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint(
            "target_kind IN ('workflow_run', 'qc_decision', 'statistical_result')",
            name="ck_work_task_reconciliation_target_kind",
        ),
    )
    op.create_index(
        "ix_work_task_reconciliation_target_run",
        "work_task_reconciliation_targets",
        ["target_kind", "run_id"],
    )
    op.create_table(
        "work_task_reconciliation_claims",
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
            name="ck_work_task_reconciliation_claim_shape",
        ),
        sa.CheckConstraint("available_at >= 0", name="ck_work_task_reconciliation_available_at"),
    )
    op.create_index(
        "ix_work_task_reconciliation_claim_schedule",
        "work_task_reconciliation_claims",
        ["available_at", "lease_expires_at"],
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT task_id, status, waiting_condition_json, authority_refs_json "
            "FROM work_tasks WHERE waiting_condition_json IS NOT NULL"
        )
    ).mappings()
    targets: list[dict[str, Any]] = []
    for row in rows:
        waiting = json.loads(row["waiting_condition_json"])
        authority = json.loads(row["authority_refs_json"])
        run_id = authority.get("workflow_run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        reference = waiting.get("reference")
        kind = waiting.get("kind")
        target_kind: str | None = None
        if row["status"] == "waiting_run" and kind == "workflow_run" and reference == run_id:
            target_kind = "workflow_run"
        elif (
            row["status"] == "waiting_user"
            and kind == "user_decision"
            and reference == f"qc:{run_id}"
        ):
            target_kind = "qc_decision"
        elif (
            row["status"] == "waiting_dependency"
            and kind == "dependency"
            and reference == f"statistical-result:{run_id}"
        ):
            target_kind = "statistical_result"
        if target_kind is not None:
            targets.append(
                {"task_id": row["task_id"], "run_id": run_id, "target_kind": target_kind}
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
    op.drop_index(
        "ix_work_task_reconciliation_claim_schedule", table_name="work_task_reconciliation_claims"
    )
    op.drop_table("work_task_reconciliation_claims")
    op.drop_index(
        "ix_work_task_reconciliation_target_run",
        table_name="work_task_reconciliation_targets",
    )
    op.drop_table("work_task_reconciliation_targets")
