"""Bind workspace file consent to a model profile revision.

Revision ID: 0017_workspace_consent_profile_revision
Revises: 0016_workspace_file_assistant
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_workspace_consent_profile_revision"
down_revision: str | None = "0016_workspace_file_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_content_consents",
        sa.Column("profile_revision", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("workspace_content_consents", "profile_revision")
