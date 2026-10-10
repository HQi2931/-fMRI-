"""Register independently reviewed external analysis inputs.

Revision ID: 0019_external_analysis_imports
Revises: 0018_work_agent_role_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_external_analysis_imports"
down_revision: str | None = "0018_work_agent_role_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_analysis_imports",
        sa.Column("import_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_tree_hash", sa.String(length=64), nullable=False),
        sa.Column("inspection_json", sa.Text(), nullable=False),
        sa.Column("inclusion_json", sa.Text(), nullable=True),
        sa.Column("inclusion_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("import_id"),
    )
    op.create_index(
        "ix_external_analysis_imports_project_id",
        "external_analysis_imports",
        ["project_id"],
    )
    op.create_index(
        "ix_external_analysis_imports_status",
        "external_analysis_imports",
        ["status"],
    )
    op.create_index(
        "ix_external_analysis_imports_source_tree_hash",
        "external_analysis_imports",
        ["source_tree_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_analysis_imports_source_tree_hash",
        table_name="external_analysis_imports",
    )
    op.drop_index("ix_external_analysis_imports_status", table_name="external_analysis_imports")
    op.drop_index("ix_external_analysis_imports_project_id", table_name="external_analysis_imports")
    op.drop_table("external_analysis_imports")
