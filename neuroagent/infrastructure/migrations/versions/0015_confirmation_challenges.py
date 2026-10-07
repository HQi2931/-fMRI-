"""Persist one-time confirmations for real Work execution."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_confirmation_challenges"
down_revision: str | None = "0014_agent_tool_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confirmation_challenges",
        sa.Column("confirmation_id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.project_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "plan_revision_id",
            sa.String(36),
            sa.ForeignKey("plan_revisions.plan_revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("qc_mode", sa.String(20), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_run_id",
            sa.String(36),
            sa.ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_confirmation_challenges_conversation",
        "confirmation_challenges",
        ["conversation_id", "issued_at"],
    )
    op.create_index(
        "ix_confirmation_challenges_plan_hash",
        "confirmation_challenges",
        ["project_id", "plan_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_confirmation_challenges_plan_hash", table_name="confirmation_challenges")
    op.drop_index("ix_confirmation_challenges_conversation", table_name="confirmation_challenges")
    op.drop_table("confirmation_challenges")
