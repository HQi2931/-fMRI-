"""Memory importance and explicit expiry."""

import sqlalchemy as sa
from alembic import op

revision = "0012_memory_lifecycle"
down_revision = "0011_memory_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_records", sa.Column("importance", sa.Float(), nullable=False, server_default="0.5")
    )
    op.add_column(
        "memory_records", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("memory_records", sa.Column("proposed_content", sa.Text(), nullable=True))
    op.add_column(
        "memory_records", sa.Column("proposal_source_message_id", sa.String(36), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("memory_records", "proposal_source_message_id")
    op.drop_column("memory_records", "proposed_content")
    op.drop_column("memory_records", "expires_at")
    op.drop_column("memory_records", "importance")
