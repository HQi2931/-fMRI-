"""Record bounded Agent tool-loop steps on the conversation audit trail.

A single user turn can now invoke several tools, so the audit row needs to know
where in the loop it happened, how much authority the tool carried and how long
it took.

Revision ID: 0014_agent_tool_loop
Revises: 0013_optional_qc
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_agent_tool_loop"
down_revision: str | None = "0013_optional_qc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("duration_ms", "provider_call_id", "risk_tier", "step_index")


def upgrade() -> None:
    # Nullable so every pre-existing row stays valid: rows written before the
    # loop existed have no step, tier or provider call id.
    with op.batch_alter_table("conversation_tool_calls") as batch:
        batch.add_column(sa.Column("step_index", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("risk_tier", sa.String(16), nullable=True))
        batch.add_column(sa.Column("provider_call_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.create_index(
        "ix_conversation_tool_calls_step",
        "conversation_tool_calls",
        ["conversation_id", "step_index"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_tool_calls_step",
        table_name="conversation_tool_calls",
    )
    with op.batch_alter_table("conversation_tool_calls") as batch:
        for column in _COLUMNS:
            batch.drop_column(column)
