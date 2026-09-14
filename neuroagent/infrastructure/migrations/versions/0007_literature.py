"""Add papers, detected sections, and traceable scientific chunks.

Revision ID: 0007_literature
Revises: 0006_conversations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_literature"
down_revision: str | None = "0006_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("paper_id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("authors_json", sa.Text(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("journal", sa.String(500), nullable=True),
        sa.Column("doi", sa.String(300), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_papers_year", "papers", ["year"])
    op.create_index("ix_papers_doi", "papers", ["doi"])
    op.create_index("ix_papers_source_sha256", "papers", ["source_sha256"])
    op.create_table(
        "paper_sections",
        sa.Column("section_id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("section", sa.String(200), nullable=False),
        sa.Column("subsection", sa.String(200), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("paragraphs_json", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.paper_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_paper_sections_paper_id", "paper_sections", ["paper_id"])
    op.create_index("ix_paper_sections_section", "paper_sections", ["section"])
    op.create_index("ix_paper_sections_subsection", "paper_sections", ["subsection"])
    op.create_index("ix_paper_sections_page_start", "paper_sections", ["page_start"])
    op.create_table(
        "literature_chunks",
        sa.Column("chunk_id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("section", sa.String(200), nullable=False),
        sa.Column("subsection", sa.String(200), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.paper_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("paper_id", "chunk_index"),
    )
    op.create_index("ix_literature_chunks_paper_id", "literature_chunks", ["paper_id"])
    op.create_index("ix_literature_chunks_section", "literature_chunks", ["section"])
    op.create_index("ix_literature_chunks_subsection", "literature_chunks", ["subsection"])
    op.create_index("ix_literature_chunks_page_start", "literature_chunks", ["page_start"])


def downgrade() -> None:
    op.drop_index("ix_literature_chunks_page_start", table_name="literature_chunks")
    op.drop_index("ix_literature_chunks_subsection", table_name="literature_chunks")
    op.drop_index("ix_literature_chunks_section", table_name="literature_chunks")
    op.drop_index("ix_literature_chunks_paper_id", table_name="literature_chunks")
    op.drop_table("literature_chunks")
    op.drop_index("ix_paper_sections_page_start", table_name="paper_sections")
    op.drop_index("ix_paper_sections_subsection", table_name="paper_sections")
    op.drop_index("ix_paper_sections_section", table_name="paper_sections")
    op.drop_index("ix_paper_sections_paper_id", table_name="paper_sections")
    op.drop_table("paper_sections")
    op.drop_index("ix_papers_source_sha256", table_name="papers")
    op.drop_index("ix_papers_doi", table_name="papers")
    op.drop_index("ix_papers_year", table_name="papers")
    op.drop_table("papers")
