"""Persist section order, including databases initialized during phase one."""

import sqlalchemy as sa
from alembic import op

revision = "0008_section_order"
down_revision = "0007_literature"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "section_index" in {c["name"] for c in sa.inspect(bind).get_columns("paper_sections")}:
        return
    op.add_column(
        "paper_sections",
        sa.Column("section_index", sa.Integer(), nullable=False, server_default="0"),
    )
    rows = bind.execute(
        sa.text(
            "SELECT section_id, paper_id FROM paper_sections "
            "ORDER BY paper_id, page_start, section_id"
        )
    )
    indices: dict[str, int] = {}
    for section_id, paper_id in rows:
        index = indices.get(paper_id, 0)
        bind.execute(
            sa.text("UPDATE paper_sections SET section_index=:idx WHERE section_id=:sid"),
            {"idx": index, "sid": section_id},
        )
        indices[paper_id] = index + 1
    op.create_index(
        "uq_paper_section_order", "paper_sections", ["paper_id", "section_index"], unique=True
    )


def downgrade() -> None:
    # No-op: retain provenance order rather than discard it on downgrade.
    pass
