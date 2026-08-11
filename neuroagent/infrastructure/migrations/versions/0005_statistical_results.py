"""Register deterministic statistical reproducibility reports.

Revision ID: 0005_statistical_results
Revises: 0004_idempotency_leases
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_statistical_results"
down_revision: str | None = "0004_idempotency_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "statistical_results",
        sa.Column("result_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("design_revision_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(40), nullable=False),
        sa.Column("non_scientific", sa.Boolean(), nullable=False),
        sa.Column("non_scientific_reason", sa.Text(), nullable=True),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("report_markdown", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_statistical_results_run_id", "statistical_results", ["run_id"])
    op.create_index("ix_statistical_results_project_id", "statistical_results", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_statistical_results_project_id", table_name="statistical_results")
    op.drop_index("ix_statistical_results_run_id", table_name="statistical_results")
    op.drop_table("statistical_results")
