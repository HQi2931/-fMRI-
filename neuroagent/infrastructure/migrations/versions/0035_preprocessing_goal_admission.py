"""Allow a composite preprocessing goal root before its WorkTask exists.

Revision ID: 0035_preprocessing_goal_admission
Revises: 0034_analysis_only_result_outbox
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_preprocessing_goal_admission"
down_revision: str | None = "0034_analysis_only_result_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "composite_goal_model_call_reservations",
    "composite_goal_model_budgets",
    "composite_goal_analysis_attempts",
    "composite_goal_links",
)
_OLD = "AND preprocessing_task_id IS NOT NULL AND source_external_import_id IS NULL"
_NEW = "AND source_external_import_id IS NULL"


def upgrade() -> None:
    _replace_preprocessing_requirement(_OLD, _NEW)


def downgrade() -> None:
    unbound = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM composite_goal_links "
                "WHERE goal_kind='preprocess_then_analyze' "
                "AND preprocessing_task_id IS NULL LIMIT 1"
            )
        )
        .first()
    )
    if unbound is not None:
        raise RuntimeError(
            "Cannot downgrade 0035 while an admitted preprocessing goal has no bound Task; "
            "the previous schema cannot preserve it."
        )
    _replace_preprocessing_requirement(_NEW, _OLD)


def _replace_preprocessing_requirement(old: str, new: str) -> None:
    """Rebuild the root and its dependent tables without dropping goal history."""

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
        raise RuntimeError("Unexpected composite goal source constraint during 0035 migration")
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
        connection.exec_driver_sql(f"CREATE TEMP TABLE __0035_{table} AS SELECT * FROM {table}")
    for table in _TABLES:
        op.drop_table(table)
    for table in reversed(_TABLES):
        connection.exec_driver_sql(definitions[table])
        connection.exec_driver_sql(f"INSERT INTO {table} SELECT * FROM __0035_{table}")
    for sql in adjuncts:
        connection.exec_driver_sql(sql)
    for table in _TABLES:
        connection.exec_driver_sql(f"DROP TABLE __0035_{table}")
