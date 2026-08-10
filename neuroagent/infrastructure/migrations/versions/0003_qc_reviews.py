"""Add immutable QC review revisions and append-only approvals.

Revision ID: 0003_qc_reviews
Revises: 0002_agent_profiles
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_qc_reviews"
down_revision: str | None = "0002_agent_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qc_review_revisions",
        sa.Column("review_revision_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("run_id", "revision"),
    )
    op.create_index("ix_qc_review_revisions_run_id", "qc_review_revisions", ["run_id"])
    op.create_index("ix_qc_review_revisions_project_id", "qc_review_revisions", ["project_id"])
    op.create_index("ix_qc_review_revisions_state", "qc_review_revisions", ["state"])
    op.create_index("ix_qc_review_revisions_content_hash", "qc_review_revisions", ["content_hash"])
    op.create_table(
        "qc_approval_records",
        sa.Column("qc_approval_id", sa.String(36), primary_key=True),
        sa.Column("review_revision_id", sa.String(36), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_revision_id"],
            ["qc_review_revisions.review_revision_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_qc_approval_records_review_revision_id",
        "qc_approval_records",
        ["review_revision_id"],
    )


def downgrade() -> None:
    op.drop_table("qc_approval_records")
    op.drop_table("qc_review_revisions")
