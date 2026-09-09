"""Add persistent summaries, memories, and context snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0010_context_engine"
down_revision = "0009_literature_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_summary_revisions",
        sa.Column("summary_id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("covered_sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_context_summary_revisions_conversation_id",
        "context_summary_revisions",
        ["conversation_id"],
    )
    op.create_table(
        "memory_records",
        sa.Column("memory_id", sa.String(36), primary_key=True),
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
            nullable=True,
        ),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "scope", "kind", "key"),
    )
    op.create_index("ix_memory_records_conversation_id", "memory_records", ["conversation_id"])
    op.create_index("ix_memory_records_project_id", "memory_records", ["project_id"])
    op.create_index("ix_memory_records_scope", "memory_records", ["scope"])
    op.create_index("ix_memory_records_kind", "memory_records", ["kind"])
    op.create_index("ix_memory_records_status", "memory_records", ["status"])
    op.create_table(
        "context_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            sa.String(36),
            sa.ForeignKey("conversation_messages.message_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(63), nullable=True),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_context_snapshots_conversation_id", "context_snapshots", ["conversation_id"]
    )
    op.create_index("ix_context_snapshots_context_hash", "context_snapshots", ["context_hash"])


def downgrade() -> None:
    op.drop_index("ix_context_snapshots_context_hash", table_name="context_snapshots")
    op.drop_index("ix_context_snapshots_conversation_id", table_name="context_snapshots")
    op.drop_table("context_snapshots")
    op.drop_index("ix_memory_records_status", table_name="memory_records")
    op.drop_index("ix_memory_records_kind", table_name="memory_records")
    op.drop_index("ix_memory_records_scope", table_name="memory_records")
    op.drop_index("ix_memory_records_project_id", table_name="memory_records")
    op.drop_index("ix_memory_records_conversation_id", table_name="memory_records")
    op.drop_table("memory_records")
    op.drop_index(
        "ix_context_summary_revisions_conversation_id",
        table_name="context_summary_revisions",
    )
    op.drop_table("context_summary_revisions")
