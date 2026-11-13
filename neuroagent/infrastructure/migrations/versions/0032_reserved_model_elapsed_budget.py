"""Precharge elapsed allowance while a composite-goal model call is in flight.

Revision ID: 0032_reserved_model_elapsed_budget
Revises: 0031_composite_goal_model_budgets
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_reserved_model_elapsed_budget"
down_revision: str | None = "0031_composite_goal_model_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("composite_goal_model_call_reservations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reserved_elapsed_seconds",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "ck_composite_goal_model_reservation_reserved_elapsed",
            "reserved_elapsed_seconds >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("composite_goal_model_call_reservations") as batch_op:
        batch_op.drop_constraint(
            "ck_composite_goal_model_reservation_reserved_elapsed",
            type_="check",
        )
        batch_op.drop_column("reserved_elapsed_seconds")
