"""Allow exact result interpretation links on external-import analysis goals.

Revision ID: 0034_analysis_only_result_outbox
Revises: 0033_analysis_only_composite_goals
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_analysis_only_result_outbox"
down_revision: str | None = "0033_analysis_only_composite_goals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "composite_goal_model_call_reservations",
    "composite_goal_model_budgets",
    "composite_goal_analysis_attempts",
    "composite_goal_links",
)
_OLD = "AND result_interpretation_outbox_task_id IS NULL)"
_NEW = (
    "AND (result_interpretation_outbox_task_id IS NULL "
    "OR (analysis_work_task_id IS NOT NULL "
    "AND result_interpretation_outbox_task_id = analysis_work_task_id)))"
)


def upgrade() -> None:
    _replace_source_constraint(_OLD, _NEW)


def downgrade() -> None:
    linked = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM composite_goal_links "
                "WHERE goal_kind='analysis_only_external_import' "
                "AND result_interpretation_outbox_task_id IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if linked is not None:
        raise RuntimeError(
            "Cannot downgrade 0034 while analysis-only result interpretation links exist; "
            "the previous schema cannot preserve them."
        )
    _replace_source_constraint(_NEW, _OLD)


def _replace_source_constraint(old: str, new: str) -> None:
    """Preserve every root/attempt/budget/reservation and FK during SQLite rebuild."""
    connection = op.get_bind()
    definitions = {
        table: connection.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:table"),
            {"table": table},
        ).scalar_one()
        for table in _TABLES
    }
    root_sql = definitions["composite_goal_links"]
    if old not in root_sql:
        # Database.initialize may have created the latest ORM shape already.
        if new in root_sql:
            return
        raise RuntimeError("Unexpected composite goal source constraint during 0034 migration")
    definitions["composite_goal_links"] = root_sql.replace(old, new, 1)
    table_list = ", ".join(f"'{table}'" for table in _TABLES)
    adjuncts = (
        connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
                f"AND tbl_name IN ({table_list}) AND sql IS NOT NULL ORDER BY type, name"
            )
        )
        .scalars()
        .all()
    )
    for table in _TABLES:
        connection.exec_driver_sql(f"CREATE TEMP TABLE __0034_{table} AS SELECT * FROM {table}")
    for table in _TABLES:
        op.drop_table(table)
    for table in reversed(_TABLES):
        connection.exec_driver_sql(definitions[table])
        connection.exec_driver_sql(f"INSERT INTO {table} SELECT * FROM __0034_{table}")
    for sql in adjuncts:
        connection.exec_driver_sql(sql)
    for table in _TABLES:
        connection.exec_driver_sql(f"DROP TABLE __0034_{table}")
