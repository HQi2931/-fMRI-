"""Persist explicit optional-QC run decisions.

Revision ID: 0013_optional_qc
Revises: 0012_memory_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_optional_qc"
down_revision: str | None = "0012_memory_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("qc_mode", sa.String(20), nullable=True))
    op.add_column("workflow_runs", sa.Column("qc_status", sa.String(32), nullable=True))
    op.execute("UPDATE workflow_runs SET qc_mode = 'require' WHERE qc_mode IS NULL")
    op.execute("UPDATE workflow_runs SET qc_status = 'pending' WHERE qc_status IS NULL")
    op.create_table(
        "qc_skip_records",
        sa.Column("qc_skip_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("qc_review_revision_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("run_id"),
        sa.UniqueConstraint("decision_hash"),
    )
    op.create_index("ix_qc_skip_records_project_id", "qc_skip_records", ["project_id"])


def downgrade() -> None:
    op.drop_table("qc_skip_records")
    op.drop_column("workflow_runs", "qc_status")
    op.drop_column("workflow_runs", "qc_mode")
