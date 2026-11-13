"""Persist proposal-only interpretation continuations for real results.

Revision ID: 0027_statistical_result_interpretation_outbox
Revises: 0026_analysis_outbox_consumer
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_statistical_result_interpretation_outbox"
down_revision: str | None = "0026_analysis_outbox_consumer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "statistical_result_interpretation_outbox",
        sa.Column("task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("plan_revision_id", sa.String(100), nullable=False),
        sa.Column("result_id", sa.String(100), nullable=False),
        sa.Column("result_bundle_hash", sa.String(64), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("continuation_message_id", sa.String(36), nullable=True),
        sa.CheckConstraint(
            "status IN ('ready', 'completed', 'waiting_user', 'blocked')",
            name="ck_result_interpretation_status",
        ),
        sa.CheckConstraint(
            "(status = 'ready' AND continuation_message_id IS NULL) OR "
            "(status != 'ready' AND continuation_message_id IS NOT NULL)",
            name="ck_result_interpretation_terminal_message",
        ),
        sa.CheckConstraint(
            "fencing_token >= 0",
            name="ck_result_interpretation_fencing_nonnegative",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_result_interpretation_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "retry_generation >= 0",
            name="ck_result_interpretation_generation_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_result_interpretation_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["continuation_message_id"],
            ["conversation_messages.message_id"],
            ondelete="RESTRICT",
            name="fk_result_interpretation_message",
        ),
        sa.UniqueConstraint("result_id", name="uq_result_interpretation_result"),
        sa.UniqueConstraint("continuation_message_id", name="uq_result_interpretation_message"),
    )
    op.create_index(
        "ix_result_interpretation_claim_due",
        "statistical_result_interpretation_outbox",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_result_interpretation_assistant_insert
        BEFORE INSERT ON statistical_result_interpretation_outbox
        WHEN NEW.continuation_message_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE message_id = NEW.continuation_message_id
                  AND conversation_id = NEW.conversation_id
                  AND role = 'assistant'
            ) THEN RAISE(
                ABORT, 'result interpretation continuation must be an assistant message'
            ) END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_result_interpretation_assistant_update
        BEFORE UPDATE OF continuation_message_id, conversation_id
        ON statistical_result_interpretation_outbox
        WHEN NEW.continuation_message_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE message_id = NEW.continuation_message_id
                  AND conversation_id = NEW.conversation_id
                  AND role = 'assistant'
            ) THEN RAISE(
                ABORT, 'result interpretation continuation must be an assistant message'
            ) END;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_result_interpretation_assistant_update")
    op.execute("DROP TRIGGER IF EXISTS trg_result_interpretation_assistant_insert")
    op.drop_index(
        "ix_result_interpretation_claim_due",
        table_name="statistical_result_interpretation_outbox",
    )
    op.drop_table("statistical_result_interpretation_outbox")
