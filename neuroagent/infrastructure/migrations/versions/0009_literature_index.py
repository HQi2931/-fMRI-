"""Persist explicit literature indexing state."""

import sqlalchemy as sa
from alembic import op

revision = "0009_literature_index"
down_revision = "0008_section_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "papers",
        sa.Column("index_status", sa.String(20), nullable=False, server_default="not_indexed"),
    )
    op.add_column("papers", sa.Column("index_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "index_error")
    op.drop_column("papers", "index_status")
