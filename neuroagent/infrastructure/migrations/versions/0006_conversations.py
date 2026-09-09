"""Persist multi-turn Agent conversations and tool calls.

Revision ID: 0006_conversations
Revises: 0005_statistical_results
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_conversations"
down_revision: str | None = "0005_statistical_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("preferred_profile_id", sa.String(63), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("active_run_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["active_run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_conversations_mode", "conversations", ["mode"])
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.conversation_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("conversation_id", "sequence"),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )
    op.create_table(
        "conversation_tool_calls",
        sa.Column("tool_call_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_message_id", sa.String(36), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.conversation_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"], ["conversation_messages.message_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_conversation_tool_calls_conversation_id",
        "conversation_tool_calls",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_tool_calls_user_message_id",
        "conversation_tool_calls",
        ["user_message_id"],
    )
    op.create_index("ix_conversation_tool_calls_status", "conversation_tool_calls", ["status"])


def downgrade() -> None:
    op.drop_index("ix_conversation_tool_calls_status", table_name="conversation_tool_calls")
    op.drop_index(
        "ix_conversation_tool_calls_user_message_id", table_name="conversation_tool_calls"
    )
    op.drop_index(
        "ix_conversation_tool_calls_conversation_id", table_name="conversation_tool_calls"
    )
    op.drop_table("conversation_tool_calls")
    op.drop_index("ix_conversation_messages_conversation_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_mode", table_name="conversations")
    op.drop_table("conversations")
