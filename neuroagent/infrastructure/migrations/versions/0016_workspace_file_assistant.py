"""Consent and immutable patch records for the Work workspace assistant."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_workspace_file_assistant"
down_revision: str | None = "0015_confirmation_challenges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_content_consents",
        sa.Column("consent_id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_hash", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(63), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("provider_host", sa.String(255), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "conversation_id",
            "workspace_hash",
            "profile_id",
            "model",
            "policy_version",
            name="uq_workspace_consent_scope",
        ),
    )
    op.create_index(
        "ix_workspace_consent_conversation",
        "workspace_content_consents",
        ["conversation_id", "workspace_hash"],
    )
    op.create_table(
        "workspace_change_proposals",
        sa.Column("proposal_id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_hash", sa.String(64), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("edits_json", sa.Text(), nullable=False),
        sa.Column("diff_text", sa.Text(), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("backup_dir", sa.Text(), nullable=True),
        sa.Column("postimage_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workspace_patch_conversation",
        "workspace_change_proposals",
        ["conversation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_patch_conversation", table_name="workspace_change_proposals")
    op.drop_table("workspace_change_proposals")
    op.drop_index("ix_workspace_consent_conversation", table_name="workspace_content_consents")
    op.drop_table("workspace_content_consents")
