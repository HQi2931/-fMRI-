"""SQLite persistence for long-lived user-goal automation records."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    WorkEventIdentity,
    WorkTask,
    advance_event_cursor,
    decide_event_consumption,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.sqlite_adapter import SqliteAdapterMixin
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


class SqliteWorkTaskRepository(SqliteAdapterMixin):
    """Versioned WorkTask store sharing the application's SQLite transaction."""

    def __init__(self, database: Database, *, unit_of_work: SqliteUnitOfWork) -> None:
        super().__init__(database, unit_of_work=unit_of_work)

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        active = self.unit_of_work.current_session()
        if active is not None:
            active.flush()
            yield active
        else:
            with self.database.session_factory() as session:
                yield session

    def create_task(self, task: WorkTask) -> WorkTask:
        with self.unit_of_work.atomic(), self._write_session() as session:
            existing = session.execute(
                text("SELECT 1 FROM work_tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            ).first()
            if existing is not None:
                raise ConflictError(
                    "work_task_exists", "该自动化任务已存在。", task_id=task.task_id
                )
            session.execute(
                text(
                    """INSERT INTO work_tasks
                    (task_id, status, version, goal_json, waiting_condition_json,
                     authority_refs_json, recovery_steps_json, budget_json)
                    VALUES (:task_id, :status, :version, :goal, :waiting,
                            :authority_refs, :recovery_steps, :budget)"""
                ),
                self._task_values(task),
            )
            self._replace_children(session, task)
        return task

    def get_task(self, task_id: str) -> WorkTask:
        with self._read_session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM work_tasks WHERE task_id = :task_id"),
                    {"task_id": task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("自动化任务", task_id)
            step_rows = session.execute(
                text(
                    "SELECT step_json FROM work_task_steps WHERE task_id = :task_id "
                    "ORDER BY sequence"
                ),
                {"task_id": task_id},
            ).mappings()
            cursor_rows = session.execute(
                text(
                    "SELECT stream_key, event_id FROM work_task_event_cursors "
                    "WHERE task_id = :task_id ORDER BY stream_key"
                ),
                {"task_id": task_id},
            ).mappings()
            return WorkTask.model_validate(
                {
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "version": row["version"],
                    "goal": json.loads(row["goal_json"]),
                    "waiting_condition": (
                        json.loads(row["waiting_condition_json"])
                        if row["waiting_condition_json"]
                        else None
                    ),
                    "authority_refs": json.loads(row["authority_refs_json"]),
                    "recovery_steps": json.loads(row["recovery_steps_json"]),
                    "budget": json.loads(row["budget_json"]),
                    "steps": [json.loads(item["step_json"]) for item in step_rows],
                    "event_cursors": [dict(item) for item in cursor_rows],
                }
            )

    def save_task(self, task: WorkTask, *, expected_version: int) -> WorkTask:
        if task.version != expected_version + 1:
            raise ValueError("saved WorkTask version must equal expected_version + 1")
        with self.unit_of_work.atomic(), self._write_session() as session:
            changed = self._update_task_row(session, task, expected_version)
            if changed != 1:
                self._raise_version_conflict(session, task.task_id, expected_version)
            self._replace_children(session, task)
        return task

    def consume_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        expected_version: int,
        updated_task: WorkTask,
    ) -> tuple[EventConsumptionDecision, WorkTask | None]:
        """Atomically dedupe, version-check, persist task progress and advance cursor."""

        if updated_task.task_id != task_id or updated_task.version != expected_version + 1:
            raise ValueError("updated_task identity/version does not match event transaction")
        with self.unit_of_work.atomic(), self._write_session() as session:
            duplicate = session.execute(
                text(
                    "SELECT 1 FROM work_task_event_receipts "
                    "WHERE task_id = :task_id AND dedupe_key = :dedupe_key"
                ),
                {"task_id": task_id, "dedupe_key": event.dedupe_key},
            ).first()
            cursor = (
                session.execute(
                    text(
                        "SELECT event_id FROM work_task_event_cursors "
                        "WHERE task_id = :task_id AND stream_key = :stream_key"
                    ),
                    {"task_id": task_id, "stream_key": event.stream_key},
                ).scalar_one_or_none()
                or 0
            )
            decision = decide_event_consumption(
                event,
                cursor=cursor,
                dedupe_key_seen=duplicate is not None,
            )
            if decision is not EventConsumptionDecision.CONSUME:
                return decision, None
            changed = self._update_task_row(session, updated_task, expected_version)
            if changed != 1:
                self._raise_version_conflict(session, task_id, expected_version)
            advanced = advance_event_cursor(updated_task, event.stream_key, event.event_id)
            committed_task = WorkTask.model_validate(
                {**advanced.model_dump(), "version": updated_task.version}
            )
            self._replace_children(session, committed_task)
            session.execute(
                text(
                    """INSERT INTO work_task_event_receipts
                    (task_id, dedupe_key, stream_key, event_id)
                    VALUES (:task_id, :dedupe_key, :stream_key, :event_id)"""
                ),
                {
                    "task_id": task_id,
                    "dedupe_key": event.dedupe_key,
                    "stream_key": event.stream_key,
                    "event_id": event.event_id,
                },
            )
        return EventConsumptionDecision.CONSUME, committed_task

    @staticmethod
    def _task_values(task: WorkTask) -> dict[str, object]:
        return {
            "task_id": task.task_id,
            "status": task.status.value,
            "version": task.version,
            "goal": task.goal.model_dump_json(),
            "waiting": task.waiting_condition.model_dump_json() if task.waiting_condition else None,
            "authority_refs": task.authority_refs.model_dump_json(),
            "recovery_steps": json.dumps(task.recovery_steps),
            "budget": task.budget.model_dump_json(),
        }

    def _update_task_row(self, session: Session, task: WorkTask, expected_version: int) -> int:
        values = self._task_values(task)
        result = cast(
            CursorResult[Any],
            session.execute(
                text(
                    """UPDATE work_tasks SET status=:status, version=:version,
                    goal_json=:goal, waiting_condition_json=:waiting,
                    authority_refs_json=:authority_refs, recovery_steps_json=:recovery_steps,
                    budget_json=:budget WHERE task_id=:task_id AND version=:expected_version"""
                ),
                {**values, "expected_version": expected_version},
            ),
        )
        return result.rowcount or 0

    def _replace_children(self, session: Session, task: WorkTask) -> None:
        session.execute(
            text("DELETE FROM work_task_steps WHERE task_id = :task_id"),
            {"task_id": task.task_id},
        )
        for step in task.steps:
            session.execute(
                text(
                    """INSERT INTO work_task_steps
                    (task_id, step_id, sequence, status, version, step_json)
                    VALUES (:task_id, :step_id, :sequence, :status, :version, :step_json)"""
                ),
                {
                    "task_id": task.task_id,
                    "step_id": step.step_id,
                    "sequence": step.sequence,
                    "status": step.status.value,
                    "version": step.version,
                    "step_json": step.model_dump_json(),
                },
            )
        session.execute(
            text("DELETE FROM work_task_event_cursors WHERE task_id = :task_id"),
            {"task_id": task.task_id},
        )
        for cursor in task.event_cursors:
            session.execute(
                text(
                    """INSERT INTO work_task_event_cursors
                    (task_id, stream_key, event_id) VALUES (:task_id, :stream_key, :event_id)"""
                ),
                {
                    "task_id": task.task_id,
                    "stream_key": cursor.stream_key,
                    "event_id": cursor.event_id,
                },
            )

    @staticmethod
    def _raise_version_conflict(session: Session, task_id: str, expected_version: int) -> None:
        actual = session.execute(
            text("SELECT version FROM work_tasks WHERE task_id=:task_id"),
            {"task_id": task_id},
        ).scalar_one_or_none()
        if actual is None:
            raise NotFoundError("自动化任务", task_id)
        raise ConflictError(
            "revision_conflict",
            "自动化任务已被其他操作更新。请重新读取后再继续。",
            resource="work_task",
            resource_id=task_id,
            expected=expected_version,
            actual=actual,
        )
