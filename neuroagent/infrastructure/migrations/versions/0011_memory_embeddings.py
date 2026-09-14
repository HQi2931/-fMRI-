"""Persist version-bound semantic memory embeddings."""

import sqlalchemy as sa
from alembic import op

revision = "0011_memory_embeddings"
down_revision = "0010_context_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_embeddings",
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("memory_records.memory_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("model_identity", sa.Text(), nullable=False),
        sa.Column("vector_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("memory_embeddings")
