"""Add reclaimable ownership leases to idempotency reservations.

Revision ID: 0004_idempotency_leases
Revises: 0003_qc_reviews
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_idempotency_leases"
down_revision: str | None = "0003_qc_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch:
        batch.add_column(sa.Column("owner_token", sa.String(36), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("owner_token")
