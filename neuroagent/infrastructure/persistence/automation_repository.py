"""SQLite persistence for long-lived user-goal automation records."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxClaim,
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    WorkEventIdentity,
    WorkTask,
    WorkTaskClaim,
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

    def ensure_analysis_outbox(self, record: AnalysisProposalOutbox) -> AnalysisProposalOutbox:
        with self.unit_of_work.atomic(), self._write_session() as session:
            existing = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": record.intent_task_id},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                current = self._outbox_from_row(existing)
                if (
                    current.conversation_id != record.conversation_id
                    or current.project_id != record.project_id
                    or current.preprocessing_task_id != record.preprocessing_task_id
                    or current.plan_revision_id != record.plan_revision_id
                    or current.manifest_hash != record.manifest_hash
                    or current.run_id != record.run_id
                    or current.analysis_request_hash != record.analysis_request_hash
                ):
                    raise ConflictError(
                        "analysis_outbox_binding_conflict",
                        "复合分析续接已绑定到不同的会话、项目或预处理证据。",
                    )
                return current
            session.execute(
                text(
                    """INSERT INTO analysis_proposal_outbox
                    (intent_task_id, conversation_id, project_id, preprocessing_task_id,
                     plan_revision_id, manifest_hash, run_id, analysis_request_hash,
                     artifact_ids_json, status, lease_owner, lease_expires_at, fencing_token)
                    VALUES (:intent_id, :conversation_id, :project_id, :task_id, :plan_id,
                            :manifest_hash, :run_id, :request_hash, '[]', 'pending',
                            NULL, NULL, 0)"""
                ),
                {
                    "intent_id": record.intent_task_id,
                    "conversation_id": record.conversation_id,
                    "project_id": record.project_id,
                    "task_id": record.preprocessing_task_id,
                    "plan_id": record.plan_revision_id,
                    "manifest_hash": record.manifest_hash,
                    "run_id": record.run_id,
                    "request_hash": record.analysis_request_hash,
                },
            )
        return record

    def get_analysis_outbox(self, intent_task_id: str) -> AnalysisProposalOutbox:
        with self._read_session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("分析续接 outbox", intent_task_id)
            return self._outbox_from_row(row)

    def get_analysis_outbox_for_preprocessing_task(
        self, preprocessing_task_id: str
    ) -> AnalysisProposalOutbox | None:
        with self._read_session() as session:
            row = (
                session.execute(
                    text(
                        "SELECT * FROM analysis_proposal_outbox "
                        "WHERE preprocessing_task_id=:task_id"
                    ),
                    {"task_id": preprocessing_task_id},
                )
                .mappings()
                .first()
            )
            return self._outbox_from_row(row) if row is not None else None

    def mark_analysis_outbox_ready(
        self,
        intent_task_id: str,
        *,
        preprocessing_task_id: str,
        plan_revision_id: str,
        manifest_hash: str,
        run_id: str,
        artifact_ids: tuple[str, ...],
    ) -> AnalysisProposalOutbox:
        if not artifact_ids or len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("verified Artifact IDs must be nonempty and unique")
        with self.unit_of_work.atomic(), self._write_session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("分析续接 outbox", intent_task_id)
            current = self._outbox_from_row(row)
            if (
                current.preprocessing_task_id != preprocessing_task_id
                or current.plan_revision_id != plan_revision_id
                or current.manifest_hash != manifest_hash
                or current.run_id != run_id
            ):
                raise ConflictError(
                    "analysis_outbox_authority_mismatch",
                    "续接记录绑定了不同的预处理任务、Plan、manifest 或 Run。",
                )
            if current.status is AnalysisOutboxStatus.READY:
                if current.artifact_ids != artifact_ids:
                    raise ConflictError(
                        "analysis_outbox_artifacts_conflict", "续接记录已绑定不同的核验产物。"
                    )
                return current
            session.execute(
                text(
                    """UPDATE analysis_proposal_outbox SET status='ready',
                       artifact_ids_json=:artifact_ids
                       WHERE intent_task_id=:intent_id AND status='pending'"""
                ),
                {"artifact_ids": json.dumps(artifact_ids), "intent_id": intent_task_id},
            )
            updated = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_task_id},
                )
                .mappings()
                .one()
            )
            return self._outbox_from_row(updated)

    def claim_analysis_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None:
        owner = worker_id.strip()
        if not owner or len(owner) > 200 or lease_seconds < 1:
            raise ValueError("worker_id and positive lease_seconds are required")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            row = (
                session.execute(
                    text(
                        """SELECT intent_task_id FROM analysis_proposal_outbox
                       WHERE status='ready' AND (lease_expires_at IS NULL OR lease_expires_at<=:now)
                       ORDER BY intent_task_id LIMIT 1"""
                    ),
                    {"now": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            intent_id = str(row["intent_task_id"])
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE analysis_proposal_outbox
                       SET lease_owner=:owner, lease_expires_at=:expires,
                           fencing_token=fencing_token+1
                       WHERE intent_task_id=:intent_id AND status='ready'
                         AND (lease_expires_at IS NULL OR lease_expires_at<=:now)"""
                    ),
                    {
                        "owner": owner,
                        "expires": now + lease_seconds,
                        "intent_id": intent_id,
                        "now": now,
                    },
                ),
            ).rowcount
            if changed != 1:
                return None
            token = int(
                session.execute(
                    text(
                        "SELECT fencing_token FROM analysis_proposal_outbox "
                        "WHERE intent_task_id=:id"
                    ),
                    {"id": intent_id},
                ).scalar_one()
            )
        return AnalysisOutboxClaim(
            intent_task_id=intent_id,
            worker_id=owner,
            lease_expires_at=now + lease_seconds,
            fencing_token=token,
        )

    def validate_analysis_outbox_claim(self, claim: AnalysisOutboxClaim) -> bool:
        with self._read_session() as session:
            row = session.execute(
                text(
                    """SELECT 1 FROM analysis_proposal_outbox
                       WHERE intent_task_id=:intent_id AND status='ready'
                         AND lease_owner=:owner AND fencing_token=:fence
                         AND lease_expires_at>:now"""
                ),
                {
                    "intent_id": claim.intent_task_id,
                    "owner": claim.worker_id,
                    "fence": claim.fencing_token,
                    "now": time.time(),
                },
            ).first()
            return row is not None

    @staticmethod
    def _outbox_from_row(row: Any) -> AnalysisProposalOutbox:
        return AnalysisProposalOutbox(
            intent_task_id=row["intent_task_id"],
            conversation_id=row["conversation_id"],
            project_id=row["project_id"],
            preprocessing_task_id=row["preprocessing_task_id"],
            plan_revision_id=row["plan_revision_id"],
            manifest_hash=row["manifest_hash"],
            run_id=row["run_id"],
            analysis_request_hash=row["analysis_request_hash"],
            artifact_ids=tuple(json.loads(row["artifact_ids_json"])),
            status=row["status"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            fencing_token=row["fencing_token"],
        )

    def save_task(
        self,
        task: WorkTask,
        *,
        expected_version: int,
        claim_token: str | None = None,
    ) -> WorkTask:
        if task.version != expected_version + 1:
            raise ValueError("saved WorkTask version must equal expected_version + 1")
        with self.unit_of_work.atomic(), self._write_session() as session:
            if claim_token is not None:
                self._require_reconciliation_claim(session, task.task_id, claim_token)
            changed = self._update_task_row(session, task, expected_version)
            if changed != 1:
                self._raise_version_conflict(session, task.task_id, expected_version)
            self._replace_children(session, task)
        return task

    def claim_next_reconciliation_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> WorkTaskClaim | None:
        """Atomically claim one indexed task whose authoritative state needs a recheck."""

        owner = worker_id.strip()
        if not owner or len(owner) > 200:
            raise ValueError("worker_id must contain 1 to 200 characters")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be nonnegative")
        now = time.time()
        claim_token = uuid4().hex
        with self.unit_of_work.atomic(), self._write_session() as session:
            task_id = session.execute(
                text(
                    """SELECT task.task_id
                    FROM work_task_reconciliation_targets AS target
                    JOIN work_tasks AS task ON task.task_id = target.task_id
                    LEFT JOIN work_task_reconciliation_claims AS claim
                      ON claim.task_id = task.task_id
                    WHERE (
                        (task.status = 'waiting_run' AND target.target_kind = 'workflow_run')
                        OR (task.status = 'waiting_dependency'
                            AND target.target_kind = 'workflow_run')
                        OR (task.status = 'waiting_user' AND target.target_kind = 'qc_decision')
                        OR (task.status = 'waiting_dependency'
                            AND target.target_kind = 'statistical_result')
                    )
                    AND (claim.task_id IS NULL OR (
                        claim.available_at <= :now
                        AND (claim.lease_expires_at IS NULL OR claim.lease_expires_at <= :now)
                    ))
                    ORDER BY COALESCE(claim.available_at, 0), task.task_id
                    LIMIT 1"""
                ),
                {"now": now},
            ).scalar_one_or_none()
            if task_id is None:
                return None
            inserted = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """INSERT INTO work_task_reconciliation_claims
                        (task_id, lease_owner, claim_token, lease_expires_at, available_at)
                        VALUES (:task_id, :owner, :token, :expires, :available)
                        ON CONFLICT(task_id) DO UPDATE SET
                            lease_owner = excluded.lease_owner,
                            claim_token = excluded.claim_token,
                            lease_expires_at = excluded.lease_expires_at,
                            available_at = excluded.available_at
                        WHERE work_task_reconciliation_claims.available_at <= :now
                          AND (work_task_reconciliation_claims.lease_expires_at IS NULL
                               OR work_task_reconciliation_claims.lease_expires_at <= :now)"""
                    ),
                    {
                        "task_id": task_id,
                        "owner": owner,
                        "token": claim_token,
                        "expires": now + lease_seconds,
                        "available": now + cooldown_seconds,
                        "now": now,
                    },
                ),
            )
            if inserted.rowcount != 1:
                return None
        return WorkTaskClaim(task_id=str(task_id), worker_id=owner, claim_token=claim_token)

    def release_reconciliation_claim(self, task_id: str, worker_id: str, claim_token: str) -> bool:
        """Release only the matching owner/token; keep the due-time backoff."""

        with self.unit_of_work.atomic(), self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE work_task_reconciliation_claims
                        SET lease_owner = NULL, claim_token = NULL, lease_expires_at = NULL
                        WHERE task_id = :task_id AND lease_owner = :owner
                          AND claim_token = :token"""
                    ),
                    {"task_id": task_id, "owner": worker_id, "token": claim_token},
                ),
            )
            return result.rowcount == 1

    def consume_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        expected_version: int,
        updated_task: WorkTask,
        claim_token: str | None = None,
    ) -> tuple[EventConsumptionDecision, WorkTask | None]:
        """Atomically dedupe, version-check, persist task progress and advance cursor."""

        if updated_task.task_id != task_id or updated_task.version != expected_version + 1:
            raise ValueError("updated_task identity/version does not match event transaction")
        with self.unit_of_work.atomic(), self._write_session() as session:
            if claim_token is not None:
                self._require_reconciliation_claim(session, task_id, claim_token)
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
        self._replace_reconciliation_target(session, task)

    @staticmethod
    def _replace_reconciliation_target(session: Session, task: WorkTask) -> None:
        session.execute(
            text("DELETE FROM work_task_reconciliation_targets WHERE task_id = :task_id"),
            {"task_id": task.task_id},
        )
        run_id = task.authority_refs.workflow_run_id
        waiting = task.waiting_condition
        if run_id is None or waiting is None:
            return
        target_kind: str | None = None
        if (
            task.status.value == "waiting_run"
            and waiting.kind.value == "workflow_run"
            and waiting.reference == run_id
        ):
            target_kind = "workflow_run"
        elif (
            task.status.value == "waiting_user"
            and waiting.kind.value == "user_decision"
            and waiting.reference == f"qc:{run_id}"
        ):
            target_kind = "qc_decision"
        elif (
            task.status.value == "waiting_dependency"
            and waiting.kind.value == "dependency"
            and waiting.reference == f"statistical-result:{run_id}"
        ):
            target_kind = "statistical_result"
        elif (
            task.status.value == "waiting_dependency"
            and waiting.kind.value == "dependency"
            and waiting.reference == f"downstream:{run_id}"
            and sum(step.kind == "preprocessing_run" for step in task.steps) == 1
            and any(
                step.kind == "preprocessing_run"
                and step.authority_refs.workflow_run_id == run_id
                and step.authority_refs.plan_revision_id == task.authority_refs.plan_revision_id
                for step in task.steps
            )
        ):
            # The shared workflow_run target kind is already indexed by 0023;
            # its precise eligibility is guarded by this preprocessing-only branch.
            target_kind = "workflow_run"
        if target_kind is not None:
            session.execute(
                text(
                    """INSERT INTO work_task_reconciliation_targets
                    (task_id, run_id, target_kind) VALUES (:task_id, :run_id, :target_kind)"""
                ),
                {"task_id": task.task_id, "run_id": run_id, "target_kind": target_kind},
            )

    @staticmethod
    def _require_reconciliation_claim(session: Session, task_id: str, claim_token: str) -> None:
        row = (
            session.execute(
                text(
                    """SELECT claim_token, lease_expires_at
                FROM work_task_reconciliation_claims
                WHERE task_id = :task_id"""
                ),
                {"task_id": task_id},
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["claim_token"] != claim_token
            or row["lease_expires_at"] is None
            or float(row["lease_expires_at"]) <= time.time()
        ):
            raise ConflictError(
                "work_task_claim_lost",
                "自动化任务领取已过期或已转交。本次恢复结果未写入。",
                task_id=task_id,
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
