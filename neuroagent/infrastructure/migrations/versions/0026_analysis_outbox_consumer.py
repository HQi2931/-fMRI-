"""Consume proposal-only analysis continuations with assistant-only records.

Revision ID: 0026_analysis_outbox_consumer
Revises: 0025_analysis_proposal_outbox
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_analysis_outbox_consumer"
down_revision: str | None = "0025_analysis_proposal_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_proposal_outbox") as batch:
        batch.drop_constraint("ck_analysis_outbox_status", type_="check")
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("retry_generation", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("next_attempt_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(64), nullable=True))
        batch.add_column(sa.Column("continuation_message_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("analysis_work_task_id", sa.String(100), nullable=True))
        batch.create_check_constraint(
            "ck_analysis_outbox_status",
            "status IN ('pending', 'ready', 'proposal_ready', 'waiting_user', 'blocked')",
        )
        batch.create_check_constraint(
            "ck_analysis_outbox_terminal_message",
            "(status IN ('pending', 'ready') AND continuation_message_id IS NULL) OR "
            "(status IN ('proposal_ready', 'waiting_user', 'blocked') "
            "AND continuation_message_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_analysis_outbox_work_task",
            "(status = 'proposal_ready' AND analysis_work_task_id IS NOT NULL) OR "
            "(status != 'proposal_ready' AND analysis_work_task_id IS NULL)",
        )
        batch.create_check_constraint(
            "ck_analysis_outbox_retry_generation", "retry_generation >= 0"
        )
        batch.create_check_constraint("ck_analysis_outbox_attempt_count", "attempt_count >= 0")
        batch.create_foreign_key(
            "fk_analysis_outbox_conversation",
            "conversations",
            ["conversation_id"],
            ["conversation_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_analysis_outbox_continuation_message",
            "conversation_messages",
            ["continuation_message_id"],
            ["message_id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_analysis_outbox_continuation_message", ["continuation_message_id"]
        )

    with op.batch_alter_table("conversation_messages") as batch:
        batch.add_column(sa.Column("continuation_key", sa.String(200), nullable=True))
        batch.create_unique_constraint(
            "uq_conversation_message_continuation_key", ["continuation_key"]
        )

    op.create_index(
        "ix_analysis_outbox_claim_due",
        "analysis_proposal_outbox",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_analysis_outbox_assistant_continuation_insert
        BEFORE INSERT ON analysis_proposal_outbox
        WHEN NEW.continuation_message_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE message_id = NEW.continuation_message_id
                  AND conversation_id = NEW.conversation_id
                  AND role = 'assistant'
            ) THEN RAISE(ABORT, 'analysis outbox continuation must be an assistant message') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_analysis_outbox_assistant_continuation_update
        BEFORE UPDATE OF continuation_message_id, conversation_id ON analysis_proposal_outbox
        WHEN NEW.continuation_message_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE message_id = NEW.continuation_message_id
                  AND conversation_id = NEW.conversation_id
                  AND role = 'assistant'
            ) THEN RAISE(ABORT, 'analysis outbox continuation must be an assistant message') END;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_analysis_outbox_assistant_continuation_update")
    op.execute("DROP TRIGGER IF EXISTS trg_analysis_outbox_assistant_continuation_insert")
    op.drop_index("ix_analysis_outbox_claim_due", table_name="analysis_proposal_outbox")
    with op.batch_alter_table("conversation_messages") as batch:
        batch.drop_constraint("uq_conversation_message_continuation_key", type_="unique")
        batch.drop_column("continuation_key")
    with op.batch_alter_table("analysis_proposal_outbox") as batch:
        batch.drop_constraint("uq_analysis_outbox_continuation_message", type_="unique")
        batch.drop_constraint("fk_analysis_outbox_conversation", type_="foreignkey")
        batch.drop_constraint("fk_analysis_outbox_continuation_message", type_="foreignkey")
        batch.drop_constraint("ck_analysis_outbox_attempt_count", type_="check")
        batch.drop_constraint("ck_analysis_outbox_retry_generation", type_="check")
        batch.drop_constraint("ck_analysis_outbox_work_task", type_="check")
        batch.drop_constraint("ck_analysis_outbox_terminal_message", type_="check")
        batch.drop_constraint("ck_analysis_outbox_status", type_="check")
        batch.drop_column("analysis_work_task_id")
        batch.drop_column("continuation_message_id")
        batch.drop_column("last_error_code")
        batch.drop_column("next_attempt_at")
        batch.drop_column("attempt_count")
        batch.drop_column("retry_generation")
        batch.create_check_constraint("ck_analysis_outbox_status", "status IN ('pending', 'ready')")
