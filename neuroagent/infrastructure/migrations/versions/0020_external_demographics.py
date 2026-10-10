"""Freeze external demographics independently of preprocessing datasets."""

import sqlalchemy as sa
from alembic import op

revision = "0020_external_demographics"
down_revision = "0019_external_analysis_imports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_demographics_revisions",
        sa.Column("demographics_id", sa.String(36), primary_key=True),
        sa.Column(
            "external_import_id",
            sa.String(36),
            sa.ForeignKey("external_analysis_imports.import_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("inclusion_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_import_id", "revision"),
    )
    op.create_index(
        "ix_external_demographics_revisions_external_import_id",
        "external_demographics_revisions",
        ["external_import_id"],
    )


def downgrade() -> None:
    op.drop_table("external_demographics_revisions")
