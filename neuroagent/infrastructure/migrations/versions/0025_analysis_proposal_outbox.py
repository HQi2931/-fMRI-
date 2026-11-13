"""Persist proposal-only analysis continuation intents.

Revision ID: 0025_analysis_proposal_outbox
Revises: 0024_preprocessing_dependency_recovery
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_analysis_proposal_outbox"
down_revision: str | None = "0024_preprocessing_dependency_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_proposal_outbox",
        sa.Column("intent_task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(100), nullable=False),
        sa.Column("preprocessing_task_id", sa.String(100), nullable=False),
        sa.Column("plan_revision_id", sa.String(100), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("analysis_request_hash", sa.String(64), nullable=False),
        sa.Column("artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("status IN ('pending', 'ready')", name="ck_analysis_outbox_status"),
        sa.CheckConstraint("fencing_token >= 0", name="ck_analysis_outbox_fencing_nonnegative"),
        sa.UniqueConstraint(
            "conversation_id",
            "project_id",
            "preprocessing_task_id",
            name="uq_analysis_outbox_prep_binding",
        ),
    )
    op.create_index(
        "ix_analysis_outbox_ready_lease",
        "analysis_proposal_outbox",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_outbox_ready_lease", table_name="analysis_proposal_outbox")
    op.drop_table("analysis_proposal_outbox")
