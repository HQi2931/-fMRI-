"""Allow multiple explicit intents to share one preprocessing task.

Revision ID: 0030_composite_goal_source_fanout
Revises: 0029_step_preparation_claims
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_composite_goal_source_fanout"
down_revision: str | None = "0029_step_preparation_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "analysis_proposal_outbox",
    "composite_goal_links",
    "composite_goal_analysis_attempts",
)
_BACKUPS = {table: f"__0030_backup_{table}" for table in _TABLES}
_REPLACEMENT_INDEXES = {
    "ix_analysis_outbox_prep_binding",
    "ix_composite_goal_preprocessing_task",
}


def upgrade() -> None:
    _rebuild_goal_tables(allow_multiple_sources=True)


def downgrade() -> None:
    connection = op.get_bind()
    duplicated_roots = connection.execute(
        sa.text(
            "SELECT 1 FROM composite_goal_links GROUP BY preprocessing_task_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    duplicated_outboxes = connection.execute(
        sa.text(
            "SELECT 1 FROM analysis_proposal_outbox "
            "GROUP BY conversation_id, project_id, preprocessing_task_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicated_roots is not None or duplicated_outboxes is not None:
        raise RuntimeError(
            "Cannot downgrade 0030 while multiple intents share a preprocessing task; "
            "the previous schema cannot represent those links."
        )
    _rebuild_goal_tables(allow_multiple_sources=False)


def _rebuild_goal_tables(*, allow_multiple_sources: bool) -> None:
    """Rebuild the small FK-connected goal tables without toggling SQLite FKs.

    Work startup runs migrations on a connection with foreign-key enforcement
    enabled. The old outbox and root tables reference one another, and attempt
    rows reference roots, so SQLite cannot batch-drop any one parent in place.
    Copying all three tables, dropping them child-first, then recreating parents
    before children keeps the operation transactional while preserving their
    rows, indexes, and triggers.
    """

    connection = op.get_bind()
    saved_indexes = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name IN (:outbox, :goals, :attempts) AND sql IS NOT NULL "
            "ORDER BY name"
        ),
        {
            "outbox": "analysis_proposal_outbox",
            "goals": "composite_goal_links",
            "attempts": "composite_goal_analysis_attempts",
        },
    ).all()
    saved_triggers = (
        connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN (:outbox, :goals, :attempts) AND sql IS NOT NULL "
                "ORDER BY name"
            ),
            {
                "outbox": "analysis_proposal_outbox",
                "goals": "composite_goal_links",
                "attempts": "composite_goal_analysis_attempts",
            },
        )
        .scalars()
        .all()
    )

    for table in _TABLES:
        backup = _BACKUPS[table]
        connection.exec_driver_sql(f"CREATE TEMP TABLE {backup} AS SELECT * FROM {table}")

    # Drop children before parents so this also works with PRAGMA foreign_keys=ON.
    op.drop_table("composite_goal_analysis_attempts")
    op.drop_table("composite_goal_links")
    op.drop_table("analysis_proposal_outbox")

    _create_outbox_table(allow_multiple_sources=allow_multiple_sources)
    _create_goal_table(allow_multiple_sources=allow_multiple_sources)
    _create_attempt_table()

    for table in (
        "analysis_proposal_outbox",
        "composite_goal_links",
        "composite_goal_analysis_attempts",
    ):
        backup = _BACKUPS[table]
        connection.exec_driver_sql(f"INSERT INTO {table} SELECT * FROM {backup}")

    for index_name, index_sql in saved_indexes:
        if index_name in _REPLACEMENT_INDEXES:
            continue
        connection.exec_driver_sql(index_sql)
    for trigger_sql in saved_triggers:
        connection.exec_driver_sql(trigger_sql)

    if allow_multiple_sources:
        op.create_index(
            "ix_analysis_outbox_prep_binding",
            "analysis_proposal_outbox",
            ["conversation_id", "project_id", "preprocessing_task_id"],
        )
        op.create_index(
            "ix_composite_goal_preprocessing_task",
            "composite_goal_links",
            ["preprocessing_task_id"],
        )

    for backup in _BACKUPS.values():
        connection.exec_driver_sql(f"DROP TABLE {backup}")


def _create_outbox_table(*, allow_multiple_sources: bool) -> None:
    constraints = [
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'proposal_ready', 'waiting_user', 'blocked')",
            name="ck_analysis_outbox_status",
        ),
        sa.CheckConstraint("fencing_token >= 0", name="ck_analysis_outbox_fencing_nonnegative"),
        sa.CheckConstraint(
            "(status IN ('pending', 'ready') AND continuation_message_id IS NULL) OR "
            "(status IN ('proposal_ready', 'waiting_user', 'blocked') "
            "AND continuation_message_id IS NOT NULL)",
            name="ck_analysis_outbox_terminal_message",
        ),
        sa.CheckConstraint(
            "(status = 'proposal_ready' AND analysis_work_task_id IS NOT NULL) OR "
            "(status != 'proposal_ready' AND analysis_work_task_id IS NULL)",
            name="ck_analysis_outbox_work_task",
        ),
        sa.CheckConstraint("retry_generation >= 0", name="ck_analysis_outbox_retry_generation"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_analysis_outbox_attempt_count"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_analysis_outbox_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["continuation_message_id"],
            ["conversation_messages.message_id"],
            ondelete="RESTRICT",
            name="fk_analysis_outbox_continuation_message",
        ),
        sa.UniqueConstraint(
            "continuation_message_id", name="uq_analysis_outbox_continuation_message"
        ),
    ]
    if not allow_multiple_sources:
        constraints.append(
            sa.UniqueConstraint(
                "conversation_id",
                "project_id",
                "preprocessing_task_id",
                name="uq_analysis_outbox_prep_binding",
            )
        )

    op.create_table(
        "analysis_proposal_outbox",
        sa.Column("intent_task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(100), nullable=False),
        sa.Column("preprocessing_task_id", sa.String(100), nullable=False),
        sa.Column("plan_revision_id", sa.String(100), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(100), nullable=False),
        sa.Column("analysis_request_hash", sa.String(64), nullable=False),
        sa.Column("artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Float(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("continuation_message_id", sa.String(36), nullable=True),
        sa.Column("analysis_work_task_id", sa.String(100), nullable=True),
        *constraints,
    )


def _create_goal_table(*, allow_multiple_sources: bool) -> None:
    constraints = [
        sa.CheckConstraint(
            "length(analysis_request_hash) = 64",
            name="ck_composite_goal_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_project",
        ),
        sa.ForeignKeyConstraint(
            ["preprocessing_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_preprocessing_task",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_proposal_outbox_intent_task_id"],
            ["analysis_proposal_outbox.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_proposal_outbox",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_work_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_analysis_task",
        ),
        sa.ForeignKeyConstraint(
            ["result_interpretation_outbox_task_id"],
            ["statistical_result_interpretation_outbox.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_result_outbox",
        ),
        sa.UniqueConstraint(
            "analysis_proposal_outbox_intent_task_id",
            name="uq_composite_goal_proposal_outbox",
        ),
        sa.UniqueConstraint(
            "result_interpretation_outbox_task_id",
            name="uq_composite_goal_result_outbox",
        ),
    ]
    if not allow_multiple_sources:
        constraints.append(
            sa.UniqueConstraint(
                "preprocessing_task_id", name="uq_composite_goal_preprocessing_task"
            )
        )

    op.create_table(
        "composite_goal_links",
        sa.Column("intent_task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("analysis_request_hash", sa.String(64), nullable=False),
        sa.Column("preprocessing_task_id", sa.String(100), nullable=False),
        sa.Column("analysis_proposal_outbox_intent_task_id", sa.String(100), nullable=True),
        sa.Column("analysis_work_task_id", sa.String(100), nullable=True),
        sa.Column("result_interpretation_outbox_task_id", sa.String(100), nullable=True),
        *constraints,
    )


def _create_attempt_table() -> None:
    op.create_table(
        "composite_goal_analysis_attempts",
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("work_task_id", sa.String(100), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("attempt_number >= 1", name="ck_composite_goal_attempt_positive"),
        sa.CheckConstraint(
            "length(proposal_hash) = 64", name="ck_composite_goal_attempt_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_links.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_attempt_goal",
        ),
        sa.ForeignKeyConstraint(
            ["work_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_attempt_work_task",
        ),
        sa.PrimaryKeyConstraint("intent_task_id", "attempt_number"),
        sa.UniqueConstraint("work_task_id", name="uq_composite_goal_attempt_task"),
        sa.UniqueConstraint(
            "intent_task_id", "proposal_hash", name="uq_composite_goal_attempt_proposal"
        ),
    )
