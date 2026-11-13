"""Allow explicit external-import analysis goals to own durable budgets.

Revision ID: 0033_analysis_only_composite_goals
Revises: 0032_reserved_model_elapsed_budget
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0033_analysis_only_composite_goals"
down_revision: str | None = "0032_reserved_model_elapsed_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "composite_goal_model_call_reservations",
    "composite_goal_model_budgets",
    "composite_goal_analysis_attempts",
    "composite_goal_links",
)
_BACKUPS = {table: f"__0033_backup_{table}" for table in _TABLES}


def upgrade() -> None:
    existing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("composite_goal_links")
    }
    # Database.initialize() creates missing tables from current ORM metadata
    # before running Alembic. Legacy-upgrade tests can therefore present this
    # table already at the target shape while replaying an older version row.
    if "goal_kind" in existing_columns:
        return
    _rebuild_composite_goal_tables(analysis_only_enabled=True)


def downgrade() -> None:
    connection = op.get_bind()
    direct_goal = connection.execute(
        sa.text(
            "SELECT 1 FROM composite_goal_links "
            "WHERE goal_kind='analysis_only_external_import' LIMIT 1"
        )
    ).first()
    if direct_goal is not None:
        raise RuntimeError(
            "Cannot downgrade 0033 while analysis-only external-import goals exist; "
            "the previous schema cannot represent them."
        )
    _rebuild_composite_goal_tables(analysis_only_enabled=False)


def _rebuild_composite_goal_tables(*, analysis_only_enabled: bool) -> None:
    """Rebuild the FK-connected goal tables while preserving all ledger rows."""

    connection = op.get_bind()
    table_list = ", ".join(f"'{table}'" for table in _TABLES)
    saved_indexes = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            f"AND tbl_name IN ({table_list}) AND sql IS NOT NULL ORDER BY name"
        )
    ).all()
    saved_triggers = (
        connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                f"AND tbl_name IN ({table_list}) AND sql IS NOT NULL ORDER BY name"
            )
        )
        .scalars()
        .all()
    )
    for table in _TABLES:
        connection.exec_driver_sql(f"CREATE TEMP TABLE {_BACKUPS[table]} AS SELECT * FROM {table}")

    # Children reference the budget and root, so remove them before rebuilding
    # the nullable preprocessing reference on the root itself.
    for table in _TABLES:
        op.drop_table(table)

    _create_goal_table(analysis_only_enabled=analysis_only_enabled)
    _create_attempt_table()
    _create_budget_table()
    _create_reservation_table()

    if analysis_only_enabled:
        connection.exec_driver_sql(
            """INSERT INTO composite_goal_links (
                intent_task_id, conversation_id, project_id, analysis_request_hash,
                goal_kind, preprocessing_task_id, source_external_import_id,
                source_import_version, source_tree_hash, source_inclusion_hash,
                source_manifest_hash, source_revision_hash,
                analysis_proposal_outbox_intent_task_id, analysis_work_task_id,
                result_interpretation_outbox_task_id
            ) SELECT intent_task_id, conversation_id, project_id, analysis_request_hash,
                'preprocess_then_analyze', preprocessing_task_id, NULL, NULL, NULL, NULL,
                NULL, NULL, analysis_proposal_outbox_intent_task_id, analysis_work_task_id,
                result_interpretation_outbox_task_id
            FROM __0033_backup_composite_goal_links"""
        )
    else:
        connection.exec_driver_sql(
            """INSERT INTO composite_goal_links (
                intent_task_id, conversation_id, project_id, analysis_request_hash,
                preprocessing_task_id, analysis_proposal_outbox_intent_task_id,
                analysis_work_task_id, result_interpretation_outbox_task_id
            ) SELECT intent_task_id, conversation_id, project_id, analysis_request_hash,
                preprocessing_task_id, analysis_proposal_outbox_intent_task_id,
                analysis_work_task_id, result_interpretation_outbox_task_id
            FROM __0033_backup_composite_goal_links"""
        )

    for table, columns in (
        (
            "composite_goal_analysis_attempts",
            "intent_task_id, attempt_number, work_task_id, proposal_hash",
        ),
        (
            "composite_goal_model_budgets",
            "intent_task_id, max_steps, max_model_calls, max_tokens, max_elapsed_seconds, "
            "used_steps, used_model_calls, used_tokens, elapsed_seconds",
        ),
        (
            "composite_goal_model_call_reservations",
            "intent_task_id, reservation_id, call_id, state, estimated_tokens, "
            "reserved_tokens, actual_tokens, elapsed_seconds, reserved_elapsed_seconds",
        ),
    ):
        connection.exec_driver_sql(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {_BACKUPS[table]}"
        )

    for _index_name, index_sql in saved_indexes:
        connection.exec_driver_sql(index_sql)
    for trigger_sql in saved_triggers:
        connection.exec_driver_sql(trigger_sql)
    for backup in _BACKUPS.values():
        connection.exec_driver_sql(f"DROP TABLE {backup}")


def _create_goal_table(*, analysis_only_enabled: bool) -> None:
    columns: list[Any] = [
        sa.Column("intent_task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("analysis_request_hash", sa.String(64), nullable=False),
    ]
    constraints: list[Any] = [
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
    ]
    if analysis_only_enabled:
        columns.extend(
            [
                sa.Column(
                    "goal_kind",
                    sa.String(40),
                    nullable=False,
                    server_default="preprocess_then_analyze",
                ),
                sa.Column("preprocessing_task_id", sa.String(100), nullable=True),
                sa.Column("source_external_import_id", sa.String(36), nullable=True),
                sa.Column("source_import_version", sa.Integer(), nullable=True),
                sa.Column("source_tree_hash", sa.String(64), nullable=True),
                sa.Column("source_inclusion_hash", sa.String(64), nullable=True),
                sa.Column("source_manifest_hash", sa.String(64), nullable=True),
                sa.Column("source_revision_hash", sa.String(64), nullable=True),
            ]
        )
        constraints.extend(
            [
                sa.CheckConstraint(
                    "goal_kind IN ('preprocess_then_analyze', 'analysis_only_external_import')",
                    name="ck_composite_goal_kind",
                ),
                sa.CheckConstraint(
                    "(goal_kind = 'preprocess_then_analyze' "
                    "AND preprocessing_task_id IS NOT NULL "
                    "AND source_external_import_id IS NULL AND source_import_version IS NULL "
                    "AND source_tree_hash IS NULL AND source_inclusion_hash IS NULL "
                    "AND source_manifest_hash IS NULL AND source_revision_hash IS NULL) OR "
                    "(goal_kind = 'analysis_only_external_import' "
                    "AND preprocessing_task_id IS NULL "
                    "AND source_external_import_id IS NOT NULL "
                    "AND source_import_version IS NOT NULL "
                    "AND source_tree_hash IS NOT NULL AND source_inclusion_hash IS NOT NULL "
                    "AND source_manifest_hash IS NOT NULL AND source_revision_hash IS NOT NULL "
                    "AND analysis_proposal_outbox_intent_task_id IS NULL "
                    "AND result_interpretation_outbox_task_id IS NULL)",
                    name="ck_composite_goal_source_kind",
                ),
                sa.ForeignKeyConstraint(
                    ["source_external_import_id"],
                    ["external_analysis_imports.import_id"],
                    ondelete="RESTRICT",
                    name="fk_composite_goal_external_import",
                ),
            ]
        )
    else:
        columns.append(sa.Column("preprocessing_task_id", sa.String(100), nullable=False))
    columns.extend(
        [
            sa.Column("analysis_proposal_outbox_intent_task_id", sa.String(100), nullable=True),
            sa.Column("analysis_work_task_id", sa.String(100), nullable=True),
            sa.Column("result_interpretation_outbox_task_id", sa.String(100), nullable=True),
        ]
    )
    constraints.extend(
        [
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
    )
    op.create_table("composite_goal_links", *columns, *constraints)


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


def _create_budget_table() -> None:
    op.create_table(
        "composite_goal_model_budgets",
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("max_elapsed_seconds", sa.Integer(), nullable=False),
        sa.Column("used_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_links.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_model_budget_goal",
        ),
        sa.PrimaryKeyConstraint("intent_task_id"),
        sa.CheckConstraint("max_steps >= 1", name="ck_composite_goal_budget_max_steps"),
        sa.CheckConstraint("max_model_calls >= 0", name="ck_composite_goal_budget_max_calls"),
        sa.CheckConstraint("max_tokens >= 0", name="ck_composite_goal_budget_max_tokens"),
        sa.CheckConstraint("max_elapsed_seconds >= 1", name="ck_composite_goal_budget_max_elapsed"),
        sa.CheckConstraint("used_steps >= 0", name="ck_composite_goal_budget_used_steps"),
        sa.CheckConstraint("used_model_calls >= 0", name="ck_composite_goal_budget_used_calls"),
        sa.CheckConstraint("used_tokens >= 0", name="ck_composite_goal_budget_used_tokens"),
        sa.CheckConstraint("elapsed_seconds >= 0", name="ck_composite_goal_budget_elapsed"),
    )


def _create_reservation_table() -> None:
    op.create_table(
        "composite_goal_model_call_reservations",
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("reservation_id", sa.String(100), nullable=False),
        sa.Column("call_id", sa.String(100), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_tokens", sa.Integer(), nullable=True),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_model_budgets.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_model_reservation_budget",
        ),
        sa.PrimaryKeyConstraint("intent_task_id", "reservation_id"),
        sa.UniqueConstraint(
            "intent_task_id", "call_id", name="uq_composite_goal_model_reservation_call"
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'settled', 'exhausted')",
            name="ck_composite_goal_model_reservation_state",
        ),
        sa.CheckConstraint(
            "estimated_tokens >= 0", name="ck_composite_goal_model_reservation_estimate"
        ),
        sa.CheckConstraint(
            "reserved_tokens >= 0", name="ck_composite_goal_model_reservation_reserved"
        ),
        sa.CheckConstraint(
            "actual_tokens IS NULL OR actual_tokens >= 0",
            name="ck_composite_goal_model_reservation_actual",
        ),
        sa.CheckConstraint(
            "elapsed_seconds >= 0", name="ck_composite_goal_model_reservation_elapsed"
        ),
        sa.CheckConstraint(
            "reserved_elapsed_seconds >= 0",
            name="ck_composite_goal_model_reservation_reserved_elapsed",
        ),
    )


def _create_reservation_indexes() -> None:
    op.create_index(
        "ix_composite_goal_model_reservation_goal",
        "composite_goal_model_call_reservations",
        ["intent_task_id", "state"],
    )
