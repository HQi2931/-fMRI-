"""Record the responsible Work agent on tool audit rows.

Revision ID: 0018_work_agent_role_audit
Revises: 0017_workspace_consent_profile_revision
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_work_agent_role_audit"
down_revision: str | None = "0017_workspace_consent_profile_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_tool_calls",
        sa.Column("agent_role", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_tool_calls", "agent_role")
