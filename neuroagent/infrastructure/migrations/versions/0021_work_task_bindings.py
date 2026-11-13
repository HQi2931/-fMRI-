"""Persist per-role Work task references without a second run state machine."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0021_work_task_bindings"
down_revision = "0020_external_demographics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "foreground_agent_role", sa.String(32), nullable=False, server_default="preprocessing"
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("work_tasks_json", sa.Text(), nullable=False, server_default="{}"),
    )
    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("conversation_id"),
        sa.column("mode"),
        sa.column("active_run_id"),
        sa.column("work_tasks_json"),
    )
    for row in connection.execute(sa.select(conversations)).mappings():
        if row["mode"] == "work" and row["active_run_id"] is not None:
            connection.execute(
                conversations.update()
                .where(conversations.c.conversation_id == row["conversation_id"])
                .values(
                    work_tasks_json=json.dumps(
                        {
                            "preprocessing": {
                                "agent_role": "preprocessing",
                                "run_id": row["active_run_id"],
                            }
                        }
                    )
                ),
            )


def downgrade() -> None:
    op.drop_column("conversations", "work_tasks_json")
    op.drop_column("conversations", "foreground_agent_role")
