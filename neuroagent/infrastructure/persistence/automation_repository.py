"""SQLite persistence for long-lived user-goal automation records."""

# ruff: noqa: RUF001

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
from neuroagent.application.automation.composite_goal import (
    CompositeGoalAnalysisAttempt,
    CompositeGoalContinuationMessage,
    CompositeGoalIdentity,
    CompositeGoalSnapshot,
)
from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    WorkEventIdentity,
    WorkTask,
    WorkTaskClaim,
    advance_event_cursor,
    decide_event_consumption,
)
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationClaim,
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
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

    @contextmanager
    def _snapshot_session(self) -> Iterator[Session]:
        """Hold a SQLite read transaction across all rows in a goal snapshot."""

        active = self.unit_of_work.current_session()
        if active is not None:
            active.flush()
            yield active
            return
        with self.database.session_factory() as session:
            # SQLAlchemy's SQLite transaction can otherwise remain logical
            # until a write. BEGIN makes the first SELECT establish the read
            # snapshot used by every subsequent child lookup.
            session.connection().exec_driver_sql("BEGIN")
            try:
                yield session
            finally:
                session.rollback()

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
            self._sync_preprocessing_composite_goal(session, task)
        return task

    def get_task(self, task_id: str) -> WorkTask:
        with self._read_session() as session:
            return self._task_from_session(session, task_id)

    @staticmethod
    def _task_from_session(session: Session, task_id: str) -> WorkTask:
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

    def ensure_preprocessing_composite_goal(self, task: WorkTask) -> CompositeGoalIdentity:
        """Create or verify the identity root for one explicit preprocessing task."""

        with self.unit_of_work.atomic(), self._write_session() as session:
            current = self._task_from_session(session, task.task_id)
            if current != task:
                raise ConflictError(
                    "composite_goal_preprocessing_task_changed",
                    "预处理任务在建立复合目标关联前已发生变化。",
                )
            return self._ensure_preprocessing_composite_goal(session, current)

    @staticmethod
    def _ensure_preprocessing_composite_goal(
        session: Session, task: WorkTask
    ) -> CompositeGoalIdentity:
        refs = task.authority_refs
        intent_id = refs.analysis_intent_task_id
        request_hash = refs.analysis_request_hash
        if intent_id is None and request_hash is None:
            raise ConflictError(
                "composite_goal_intent_missing", "预处理任务未绑定显式复合分析 intent。"
            )
        if intent_id is None or request_hash is None:
            raise ConflictError(
                "composite_goal_intent_incomplete", "复合分析 intent ID 与请求摘要必须同时存在。"
            )
        if len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
            raise ConflictError("composite_goal_hash_invalid", "复合目标请求摘要不是 SHA-256。")
        prep_steps = tuple(step for step in task.steps if step.kind == "preprocessing_run")
        if len(prep_steps) != 1 or refs.plan_revision_id is None or refs.input_revision is None:
            raise ConflictError(
                "composite_goal_preprocessing_binding_invalid",
                "复合目标必须引用唯一预处理步骤、冻结 Plan 和 manifest。",
            )
        step_refs = prep_steps[0].authority_refs
        if (
            step_refs.conversation_id != refs.conversation_id
            or step_refs.analysis_intent_task_id != intent_id
            or step_refs.analysis_request_hash != request_hash
            or step_refs.plan_revision_id != refs.plan_revision_id
            or step_refs.input_revision != refs.input_revision
        ):
            raise ConflictError(
                "composite_goal_preprocessing_step_mismatch",
                "预处理步骤与 WorkTask 的会话、intent 或输入引用不一致。",
            )
        plan = (
            session.execute(
                text(
                    "SELECT project_id, manifest_hash FROM plan_revisions "
                    "WHERE plan_revision_id=:plan_id"
                ),
                {"plan_id": refs.plan_revision_id},
            )
            .mappings()
            .first()
        )
        conversation = (
            session.execute(
                text("SELECT project_id FROM conversations WHERE conversation_id=:conversation_id"),
                {"conversation_id": refs.conversation_id},
            )
            .mappings()
            .first()
        )
        if plan is None or conversation is None or plan["manifest_hash"] != refs.input_revision:
            raise ConflictError(
                "composite_goal_preprocessing_authority_mismatch",
                "预处理 Plan、manifest 或会话不存在或互不匹配。",
            )
        project_id = str(plan["project_id"])
        if conversation["project_id"] not in {None, project_id}:
            raise ConflictError(
                "composite_goal_project_mismatch", "预处理会话与冻结 Plan 不属于同一项目。"
            )
        if refs.workflow_run_id is not None:
            run = (
                session.execute(
                    text(
                        "SELECT project_id, plan_revision_id FROM workflow_runs "
                        "WHERE run_id=:run_id"
                    ),
                    {"run_id": refs.workflow_run_id},
                )
                .mappings()
                .first()
            )
            if run is None or (
                run["project_id"] != project_id
                or run["plan_revision_id"] != refs.plan_revision_id
            ):
                raise ConflictError(
                    "composite_goal_preprocessing_run_mismatch",
                    "预处理任务绑定了其他项目或 Plan 的 Workflow Run。",
                )
        current_row = (
            session.execute(
                text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                {"intent_id": intent_id},
            )
            .mappings()
            .first()
        )
        if current_row is None:
            session.execute(
                text(
                    """INSERT INTO composite_goal_links
                    (intent_task_id, conversation_id, project_id, analysis_request_hash,
                     preprocessing_task_id, analysis_proposal_outbox_intent_task_id,
                     analysis_work_task_id, result_interpretation_outbox_task_id)
                    VALUES (:intent_id, :conversation_id, :project_id, :request_hash,
                            :preprocessing_task_id, NULL, NULL, NULL)"""
                ),
                {
                    "intent_id": intent_id,
                    "conversation_id": refs.conversation_id,
                    "project_id": project_id,
                    "request_hash": request_hash,
                    "preprocessing_task_id": task.task_id,
                },
            )
            current_row = (
                session.execute(
                    text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_id},
                )
                .mappings()
                .one()
            )
        identity = SqliteWorkTaskRepository._composite_goal_from_row(current_row)
        expected = (refs.conversation_id, project_id, request_hash, task.task_id)
        actual = (
            identity.conversation_id,
            identity.project_id,
            identity.analysis_request_hash,
            identity.preprocessing_task_id,
        )
        if actual != expected:
            raise ConflictError(
                "composite_goal_identity_conflict",
                "稳定复合目标 ID 已绑定到不同的会话、项目、请求或预处理任务。",
            )
        return identity

    @staticmethod
    def _sync_preprocessing_composite_goal(session: Session, task: WorkTask) -> None:
        if not any(step.kind == "preprocessing_run" for step in task.steps):
            return
        existing = (
            session.execute(
                text(
                    "SELECT * FROM composite_goal_links WHERE preprocessing_task_id=:task_id"
                ),
                {"task_id": task.task_id},
            )
            .mappings()
            .first()
        )
        refs = task.authority_refs
        if refs.analysis_intent_task_id is None and refs.analysis_request_hash is None:
            if existing is not None:
                raise ConflictError(
                    "composite_goal_preprocessing_detached",
                    "持久复合目标不能通过清除预处理任务引用而解除关联。",
                )
            return
        SqliteWorkTaskRepository._ensure_preprocessing_composite_goal(session, task)

    @staticmethod
    def _composite_goal_from_row(row: Any) -> CompositeGoalIdentity:
        return CompositeGoalIdentity(
            intent_task_id=row["intent_task_id"],
            conversation_id=row["conversation_id"],
            project_id=row["project_id"],
            analysis_request_hash=row["analysis_request_hash"],
            preprocessing_task_id=row["preprocessing_task_id"],
            analysis_proposal_outbox_intent_task_id=(
                row["analysis_proposal_outbox_intent_task_id"]
            ),
            analysis_work_task_id=row["analysis_work_task_id"],
            result_interpretation_outbox_task_id=row[
                "result_interpretation_outbox_task_id"
            ],
        )

    def find_composite_goal_for_analysis_task(
        self, analysis_work_task_id: str
    ) -> CompositeGoalIdentity | None:
        with self._read_session() as session:
            row = (
                session.execute(
                    text(
                        """SELECT goal.* FROM composite_goal_analysis_attempts AS attempt
                        JOIN composite_goal_links AS goal
                          ON goal.intent_task_id=attempt.intent_task_id
                        WHERE attempt.work_task_id=:task_id"""
                    ),
                    {"task_id": analysis_work_task_id},
                )
                .mappings()
                .first()
            )
            return self._composite_goal_from_row(row) if row is not None else None

    def get_composite_goal_snapshot(self, intent_task_id: str) -> CompositeGoalSnapshot:
        """Read the root, attempt identities and authoritative children consistently."""

        with self._snapshot_session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("复合自动化目标", intent_task_id)
            goal = self._composite_goal_from_row(row)
            conversation_row = (
                session.execute(
                    text(
                        "SELECT mode, project_id FROM conversations "
                        "WHERE conversation_id=:conversation_id"
                    ),
                    {"conversation_id": goal.conversation_id},
                )
                .mappings()
                .first()
            )
            preprocessing_task = self._task_from_session(session, goal.preprocessing_task_id)
            preprocessing_plan_project_id = None
            preprocessing_plan_manifest_hash = None
            plan_revision_id = preprocessing_task.authority_refs.plan_revision_id
            if plan_revision_id is not None:
                plan_row = (
                    session.execute(
                        text(
                            "SELECT project_id, manifest_hash FROM plan_revisions "
                            "WHERE plan_revision_id=:plan_id"
                        ),
                        {"plan_id": plan_revision_id},
                    )
                    .mappings()
                    .first()
                )
                if plan_row is not None:
                    preprocessing_plan_project_id = plan_row["project_id"]
                    preprocessing_plan_manifest_hash = plan_row["manifest_hash"]
            outbox = None
            proposal_analysis_task = None
            if goal.analysis_proposal_outbox_intent_task_id is not None:
                outbox_row = (
                    session.execute(
                        text(
                            "SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"
                        ),
                        {"intent_id": goal.analysis_proposal_outbox_intent_task_id},
                    )
                    .mappings()
                    .first()
                )
                if outbox_row is not None:
                    outbox = self._outbox_from_row(outbox_row)
                    if outbox.analysis_work_task_id is not None:
                        proposal_analysis_task = self._task_from_session(
                            session, outbox.analysis_work_task_id
                        )
            attempt_rows = session.execute(
                text(
                    "SELECT attempt_number, work_task_id, proposal_hash "
                    "FROM composite_goal_analysis_attempts WHERE intent_task_id=:intent_id "
                    "ORDER BY attempt_number"
                ),
                {"intent_id": intent_task_id},
            ).mappings()
            attempts = tuple(
                CompositeGoalAnalysisAttempt(
                    attempt_number=item["attempt_number"],
                    work_task_id=item["work_task_id"],
                    proposal_hash=item["proposal_hash"],
                )
                for item in attempt_rows
            )
            analysis_task = (
                self._task_from_session(session, goal.analysis_work_task_id)
                if goal.analysis_work_task_id is not None
                else None
            )
            interpretation = None
            continuation_message = None
            if goal.result_interpretation_outbox_task_id is not None:
                result_row = (
                    session.execute(
                        text(
                            "SELECT * FROM statistical_result_interpretation_outbox "
                            "WHERE task_id=:task_id"
                        ),
                        {"task_id": goal.result_interpretation_outbox_task_id},
                    )
                    .mappings()
                    .first()
                )
                if result_row is not None:
                    interpretation = self._result_interpretation_from_row(result_row)
                    if interpretation.continuation_message_id is not None:
                        message_row = (
                            session.execute(
                                text(
                                    "SELECT message_id, conversation_id, role, payload_json "
                                    "FROM conversation_messages WHERE message_id=:message_id"
                                ),
                                {"message_id": interpretation.continuation_message_id},
                            )
                            .mappings()
                            .first()
                        )
                        if message_row is not None:
                            try:
                                message_payload = json.loads(message_row["payload_json"])
                            except (TypeError, json.JSONDecodeError):
                                message_payload = {}
                            continuation_message = CompositeGoalContinuationMessage(
                                message_id=message_row["message_id"],
                                conversation_id=message_row["conversation_id"],
                                role=message_row["role"],
                                payload=(
                                    message_payload if isinstance(message_payload, dict) else {}
                                ),
                            )
            return CompositeGoalSnapshot(
                goal=goal,
                conversation_mode=(conversation_row["mode"] if conversation_row else None),
                conversation_project_id=(
                    conversation_row["project_id"] if conversation_row else None
                ),
                preprocessing_plan_revision_id=plan_revision_id,
                preprocessing_plan_project_id=preprocessing_plan_project_id,
                preprocessing_plan_manifest_hash=preprocessing_plan_manifest_hash,
                preprocessing_task=preprocessing_task,
                analysis_proposal_outbox=outbox,
                analysis_attempts=attempts,
                proposal_analysis_task=proposal_analysis_task,
                analysis_task=analysis_task,
                result_interpretation_outbox=interpretation,
                result_continuation_message=continuation_message,
            )

    def bind_composite_goal_analysis_task(
        self,
        goal: CompositeGoalIdentity,
        analysis_work_task_id: str,
        proposal_hash: str,
    ) -> CompositeGoalIdentity:
        """Make one exact statistical proposal the active composite-goal attempt."""

        with self.unit_of_work.atomic(), self._write_session() as session:
            root_row = (
                session.execute(
                    text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                    {"intent_id": goal.intent_task_id},
                )
                .mappings()
                .first()
            )
            if root_row is None:
                raise ConflictError("composite_goal_missing", "复合目标根记录不存在。")
            current_goal = self._composite_goal_from_row(root_row)
            if (
                current_goal.conversation_id != goal.conversation_id
                or current_goal.project_id != goal.project_id
                or current_goal.analysis_request_hash != goal.analysis_request_hash
                or current_goal.preprocessing_task_id != goal.preprocessing_task_id
                or current_goal.analysis_proposal_outbox_intent_task_id
                != goal.analysis_proposal_outbox_intent_task_id
                or current_goal.analysis_work_task_id != goal.analysis_work_task_id
            ):
                raise ConflictError(
                    "composite_goal_changed", "复合目标在统计提案提交前已变化，请刷新后重试。"
                )
            outbox_row = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": goal.intent_task_id},
                )
                .mappings()
                .first()
            )
            if outbox_row is None or not self._outbox_matches_goal(outbox_row, current_goal):
                raise ConflictError(
                    "composite_goal_outbox_mismatch", "分析提案 outbox 与复合目标身份不匹配。"
                )
            if outbox_row["status"] not in {"ready", "proposal_ready", "waiting_user"}:
                raise ConflictError(
                    "composite_goal_outbox_not_open",
                    "分析提案 outbox 已阻断，不能绑定新尝试。",
                )
            task = self._task_from_session(session, analysis_work_task_id)
            step_rows = tuple(step for step in task.steps if step.kind == "statistical_run")
            if (
                len(step_rows) != 1
                or step_rows[0].input_version != proposal_hash
                or task.authority_refs.conversation_id != current_goal.conversation_id
                or task.authority_refs.analysis_intent_task_id != current_goal.intent_task_id
                or task.authority_refs.analysis_request_hash != current_goal.analysis_request_hash
            ):
                raise ConflictError(
                    "composite_goal_analysis_task_mismatch",
                    "统计 WorkTask 的会话、intent、原始请求或提案哈希与目标不一致。",
                )
            existing_attempt = (
                session.execute(
                    text(
                        "SELECT attempt_number, proposal_hash "
                        "FROM composite_goal_analysis_attempts "
                        "WHERE work_task_id=:task_id"
                    ),
                    {"task_id": analysis_work_task_id},
                )
                .mappings()
                .first()
            )
            if existing_attempt is not None:
                if (
                    existing_attempt["proposal_hash"] != proposal_hash
                    or current_goal.analysis_work_task_id != analysis_work_task_id
                ):
                    raise ConflictError(
                        "composite_goal_attempt_superseded",
                        "该统计提案已被后续尝试取代，不能重新激活。",
                    )
                return current_goal
            if (
                task.status.value != "waiting_user"
                or task.authority_refs.plan_revision_id is not None
                or task.authority_refs.workflow_run_id is not None
            ):
                raise ConflictError(
                    "composite_goal_analysis_attempt_started",
                    "只有尚未绑定统计方案或运行的待审查提案可以成为目标尝试。",
                )
            attempts_count = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM composite_goal_analysis_attempts "
                        "WHERE intent_task_id=:intent_id"
                    ),
                    {"intent_id": goal.intent_task_id},
                ).scalar_one()
            )
            if current_goal.analysis_work_task_id is None:
                if attempts_count != 0:
                    raise ConflictError(
                        "composite_goal_active_attempt_missing",
                        "复合目标有历史尝试但没有当前尝试引用。",
                    )
                if outbox_row["status"] == "proposal_ready" and (
                    outbox_row["analysis_work_task_id"] != analysis_work_task_id
                ):
                    raise ConflictError(
                        "composite_goal_initial_attempt_mismatch",
                        "首个统计尝试必须对应 proposal outbox 生成的精确任务。",
                    )
                if (
                    outbox_row["status"] == "ready"
                    and outbox_row["analysis_work_task_id"] is not None
                ):
                    raise ConflictError(
                        "composite_goal_initial_attempt_mismatch",
                        "待消费的 proposal outbox 已绑定到其他统计任务。",
                    )
            else:
                active_task = self._task_from_session(session, current_goal.analysis_work_task_id)
                if (
                    active_task.status.value != "waiting_user"
                    or active_task.authority_refs.plan_revision_id is not None
                    or active_task.authority_refs.workflow_run_id is not None
                    or current_goal.result_interpretation_outbox_task_id is not None
                ):
                    raise ConflictError(
                        "composite_goal_attempt_not_replaceable",
                        "已有统计尝试已进入方案、运行或结果阶段，不能被草稿修订取代。",
                    )
            next_number = attempts_count + 1
            session.execute(
                text(
                    """INSERT INTO composite_goal_analysis_attempts
                    (intent_task_id, attempt_number, work_task_id, proposal_hash)
                    VALUES (:intent_id, :attempt_number, :task_id, :proposal_hash)"""
                ),
                {
                    "intent_id": goal.intent_task_id,
                    "attempt_number": next_number,
                    "task_id": analysis_work_task_id,
                    "proposal_hash": proposal_hash,
                },
            )
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        "UPDATE composite_goal_links SET analysis_work_task_id=:task_id "
                        "WHERE intent_task_id=:intent_id "
                        "AND analysis_work_task_id IS :expected_task_id"
                    ),
                    {
                        "task_id": analysis_work_task_id,
                        "intent_id": goal.intent_task_id,
                        "expected_task_id": goal.analysis_work_task_id,
                    },
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError(
                    "composite_goal_changed", "复合目标的当前尝试已被其他提案更新。"
                )
            updated = (
                session.execute(
                    text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                    {"intent_id": goal.intent_task_id},
                )
                .mappings()
                .one()
            )
            return self._composite_goal_from_row(updated)

    @staticmethod
    def _outbox_matches_goal(row: Any, goal: CompositeGoalIdentity) -> bool:
        return bool(
            row["intent_task_id"] == goal.intent_task_id
            and row["conversation_id"] == goal.conversation_id
            and row["project_id"] == goal.project_id
            and row["preprocessing_task_id"] == goal.preprocessing_task_id
            and row["analysis_request_hash"] == goal.analysis_request_hash
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
                self._link_analysis_outbox_to_goal(session, record)
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
            self._link_analysis_outbox_to_goal(session, record)
        return record

    @staticmethod
    def _link_analysis_outbox_to_goal(
        session: Session, record: AnalysisProposalOutbox
    ) -> None:
        try:
            task = SqliteWorkTaskRepository._task_from_session(
                session, record.preprocessing_task_id
            )
        except NotFoundError:
            # A legacy standalone proposal outbox has no durable preprocessing
            # task and therefore is not a composite-goal child.
            return
        refs = task.authority_refs
        if refs.analysis_intent_task_id is None and refs.analysis_request_hash is None:
            return
        identity = SqliteWorkTaskRepository._ensure_preprocessing_composite_goal(session, task)
        if (
            identity.intent_task_id != record.intent_task_id
            or identity.conversation_id != record.conversation_id
            or identity.project_id != record.project_id
            or identity.analysis_request_hash != record.analysis_request_hash
            or identity.preprocessing_task_id != record.preprocessing_task_id
            or refs.plan_revision_id != record.plan_revision_id
            or refs.input_revision != record.manifest_hash
        ):
            raise ConflictError(
                "composite_goal_outbox_binding_mismatch",
                "分析提案 outbox 与精确的预处理任务或原始请求不匹配。",
            )
        run = (
            session.execute(
                text(
                    "SELECT project_id, plan_revision_id FROM workflow_runs WHERE run_id=:run_id"
                ),
                {"run_id": record.run_id},
            )
            .mappings()
            .first()
        )
        if run is None or (
            run["project_id"] != record.project_id
            or run["plan_revision_id"] != record.plan_revision_id
            or refs.workflow_run_id != record.run_id
        ):
            raise ConflictError(
                "composite_goal_outbox_run_mismatch",
                "分析提案 outbox 未绑定预处理任务的精确项目、Plan 和 Run。",
            )
        if refs.conversation_id != record.conversation_id:
            raise ConflictError(
                "composite_goal_outbox_conversation_mismatch",
                "分析提案 outbox 会话与预处理任务不一致。",
            )
        existing = (
            session.execute(
                text(
                    "SELECT analysis_proposal_outbox_intent_task_id FROM composite_goal_links "
                    "WHERE intent_task_id=:intent_id"
                ),
                {"intent_id": record.intent_task_id},
            )
            .mappings()
            .one()
        )["analysis_proposal_outbox_intent_task_id"]
        if existing not in {None, record.intent_task_id}:
            raise ConflictError(
                "composite_goal_outbox_already_bound",
                "复合目标已经关联了另一条分析提案 outbox。",
            )
        session.execute(
            text(
                "UPDATE composite_goal_links "
                "SET analysis_proposal_outbox_intent_task_id=:intent_id "
                "WHERE intent_task_id=:intent_id "
                "AND analysis_proposal_outbox_intent_task_id IS NULL"
            ),
            {"intent_id": record.intent_task_id},
        )

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
            self._link_analysis_outbox_to_goal(session, current)
            if current.status is AnalysisOutboxStatus.READY:
                if current.artifact_ids != artifact_ids:
                    raise ConflictError(
                        "analysis_outbox_artifacts_conflict", "续接记录已绑定不同的核验产物。"
                    )
                return current
            if current.status is not AnalysisOutboxStatus.PENDING:
                raise ConflictError(
                    "analysis_outbox_already_consumed", "已消费的续接记录不能重新置为 READY。"
                )
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
                         AND (next_attempt_at IS NULL OR next_attempt_at<=:now)
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
                           fencing_token=fencing_token+1, next_attempt_at=NULL
                       WHERE intent_task_id=:intent_id AND status='ready'
                         AND (lease_expires_at IS NULL OR lease_expires_at<=:now)
                         AND (next_attempt_at IS NULL OR next_attempt_at<=:now)"""
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

    def renew_analysis_outbox_claim(
        self, claim: AnalysisOutboxClaim, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        expires = now + lease_seconds
        with self.unit_of_work.atomic(), self._write_session() as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE analysis_proposal_outbox
                           SET lease_expires_at=:expires
                           WHERE intent_task_id=:intent_id AND status='ready'
                             AND lease_owner=:owner AND fencing_token=:fence
                             AND lease_expires_at>:now"""
                    ),
                    {
                        "intent_id": claim.intent_task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                        "expires": expires,
                    },
                ),
            ).rowcount
            if changed != 1:
                return None
        return claim.model_copy(update={"lease_expires_at": expires})

    def defer_analysis_outbox_claim(
        self,
        claim: AnalysisOutboxClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool:
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be positive")
        if not error_code or len(error_code) > 64:
            raise ValueError("error_code must be a bounded code")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE analysis_proposal_outbox
                           SET attempt_count=attempt_count+1, last_error_code=:error_code,
                               next_attempt_at=:retry_at, lease_owner=NULL,
                               lease_expires_at=NULL
                           WHERE intent_task_id=:intent_id AND status='ready'
                             AND lease_owner=:owner AND fencing_token=:fence
                             AND lease_expires_at>:now"""
                    ),
                    {
                        "intent_id": claim.intent_task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                        "retry_at": now + retry_after_seconds,
                        "error_code": error_code,
                    },
                ),
            ).rowcount
            return changed == 1

    def complete_analysis_outbox(
        self,
        claim: AnalysisOutboxClaim,
        *,
        status: AnalysisOutboxStatus,
        continuation_message_id: str,
        analysis_work_task_id: str | None,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> AnalysisProposalOutbox:
        if status not in {
            AnalysisOutboxStatus.PROPOSAL_READY,
            AnalysisOutboxStatus.WAITING_USER,
            AnalysisOutboxStatus.BLOCKED,
        }:
            raise ValueError("outbox completion requires a terminal status")
        if not continuation_message_id:
            raise ValueError("terminal outbox completion requires an assistant message")
        if status is AnalysisOutboxStatus.PROPOSAL_READY and not analysis_work_task_id:
            raise ValueError("proposal-ready outbox completion requires a WorkTask")
        if status is not AnalysisOutboxStatus.PROPOSAL_READY and analysis_work_task_id is not None:
            raise ValueError("only a proposal-ready continuation may bind an analysis WorkTask")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            root_row = (
                session.execute(
                    text("SELECT * FROM composite_goal_links WHERE intent_task_id=:intent_id"),
                    {"intent_id": claim.intent_task_id},
                )
                .mappings()
                .first()
            )
            if root_row is not None:
                goal = self._composite_goal_from_row(root_row)
                outbox = (
                    session.execute(
                        text(
                            "SELECT * FROM analysis_proposal_outbox "
                            "WHERE intent_task_id=:intent_id"
                        ),
                        {"intent_id": claim.intent_task_id},
                    )
                    .mappings()
                    .one()
                )
                if not self._outbox_matches_goal(outbox, goal):
                    raise ConflictError(
                        "composite_goal_outbox_mismatch",
                        "分析续接完成时 outbox 与复合目标身份不匹配。",
                    )
                if status is AnalysisOutboxStatus.PROPOSAL_READY:
                    task = self._task_from_session(session, analysis_work_task_id or "")
                    attempt = (
                        session.execute(
                            text(
                                "SELECT proposal_hash FROM composite_goal_analysis_attempts "
                                "WHERE intent_task_id=:intent_id AND work_task_id=:task_id"
                            ),
                            {
                                "intent_id": goal.intent_task_id,
                                "task_id": analysis_work_task_id,
                            },
                        )
                        .mappings()
                        .first()
                    )
                    message = (
                        session.execute(
                            text(
                                "SELECT conversation_id, role, payload_json "
                                "FROM conversation_messages "
                                "WHERE message_id=:message_id"
                            ),
                            {"message_id": continuation_message_id},
                        )
                        .mappings()
                        .first()
                    )
                    try:
                        payload = json.loads(message["payload_json"]) if message is not None else {}
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    continuation = (
                        payload.get("analysis_outbox_continuation")
                        if isinstance(payload, dict)
                        else None
                    )
                    proposal = (
                        payload.get("statistical_design_proposal")
                        if isinstance(payload, dict)
                        else None
                    )
                    if (
                        goal.analysis_work_task_id != analysis_work_task_id
                        or attempt is None
                        or task.authority_refs.analysis_intent_task_id != goal.intent_task_id
                        or task.authority_refs.analysis_request_hash != goal.analysis_request_hash
                        or message is None
                        or message["conversation_id"] != goal.conversation_id
                        or message["role"] != "assistant"
                        or not isinstance(continuation, dict)
                        or continuation.get("intent_task_id") != goal.intent_task_id
                        or not isinstance(proposal, dict)
                        or proposal.get("proposal_hash") != attempt["proposal_hash"]
                    ):
                        raise ConflictError(
                            "composite_goal_initial_continuation_mismatch",
                            "首个分析提案 continuation 与当前复合目标尝试不匹配。",
                        )
                elif analysis_work_task_id is not None:
                    raise ConflictError(
                        "composite_goal_terminal_task_unexpected",
                        "没有形成提案的 continuation 不能绑定统计 WorkTask。",
                    )
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE analysis_proposal_outbox
                           SET status=:status, continuation_message_id=:message_id,
                               analysis_work_task_id=:work_task_id,
                               attempt_count=attempt_count + :record_attempt,
                               last_error_code=:error_code,
                               next_attempt_at=NULL, lease_owner=NULL, lease_expires_at=NULL
                           WHERE intent_task_id=:intent_id AND status='ready'
                             AND lease_owner=:owner AND fencing_token=:fence
                             AND lease_expires_at>:now
                             AND continuation_message_id IS NULL"""
                    ),
                    {
                        "intent_id": claim.intent_task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                        "status": status.value,
                        "message_id": continuation_message_id,
                        "work_task_id": analysis_work_task_id,
                        "error_code": error_code,
                        "record_attempt": int(record_attempt),
                    },
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError(
                    "analysis_outbox_claim_lost",
                    "分析续接 claim 已过期或被其他 Worker 取代。本次结果没有提交。",
                )
            row = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": claim.intent_task_id},
                )
                .mappings()
                .one()
            )
            return self._outbox_from_row(row)

    def requeue_analysis_outbox(
        self, intent_task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> AnalysisProposalOutbox | None:
        if not recoverable_error_codes:
            raise ValueError("manual requeue requires at least one recoverable error code")
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
                return None
            current = self._outbox_from_row(row)
            if current.status is AnalysisOutboxStatus.READY and current.retry_generation > 0:
                return current
            if (
                current.status is not AnalysisOutboxStatus.WAITING_USER
                or current.last_error_code not in recoverable_error_codes
            ):
                return None
            error_parameters = {
                f"error_{index}": code for index, code in enumerate(recoverable_error_codes)
            }
            error_placeholders = ", ".join(
                f":error_{index}" for index in range(len(recoverable_error_codes))
            )
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        f"""UPDATE analysis_proposal_outbox
                           SET status='ready', attempt_count=0,
                               retry_generation=retry_generation+1,
                               fencing_token=fencing_token+1, next_attempt_at=NULL,
                               lease_owner=NULL, lease_expires_at=NULL,
                               continuation_message_id=NULL, analysis_work_task_id=NULL,
                               last_error_code=NULL
                           WHERE intent_task_id=:intent_id AND status='waiting_user'
                             AND last_error_code IN ({error_placeholders})"""
                    ),
                    {
                        "intent_id": intent_task_id,
                        **error_parameters,
                    },
                ),
            ).rowcount
            if changed != 1:
                return None
            updated = (
                session.execute(
                    text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                    {"intent_id": intent_task_id},
                )
                .mappings()
                .one()
            )
            return self._outbox_from_row(updated)

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
            attempt_count=row["attempt_count"],
            retry_generation=row["retry_generation"],
            next_attempt_at=row["next_attempt_at"],
            last_error_code=row["last_error_code"],
            continuation_message_id=row["continuation_message_id"],
            analysis_work_task_id=row["analysis_work_task_id"],
        )

    def ensure_result_interpretation_outbox(
        self, record: StatisticalResultInterpretationOutbox
    ) -> StatisticalResultInterpretationOutbox:
        """Create one READY result continuation without duplicating task/result rows."""
        if not record.artifact_ids or len(set(record.artifact_ids)) != len(record.artifact_ids):
            raise ValueError("verified result Artifact IDs must be nonempty and unique")
        with self.unit_of_work.atomic(), self._write_session() as session:
            composite_goal = self._validate_composite_result_binding(session, record)
            existing = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id OR result_id=:result_id"
                    ),
                    {"task_id": record.task_id, "result_id": record.result_id},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                current = self._result_interpretation_from_row(existing)
                if current != record:
                    immutable = (
                        "task_id",
                        "conversation_id",
                        "project_id",
                        "run_id",
                        "plan_revision_id",
                        "result_id",
                        "result_bundle_hash",
                        "result_version",
                        "artifact_ids",
                    )
                    if any(getattr(current, key) != getattr(record, key) for key in immutable):
                        raise ConflictError(
                            "result_interpretation_binding_conflict",
                            "统计结果续接已绑定到不同的 WorkTask 或结果证据。",
                        )
                    if composite_goal is not None:
                        self._link_result_outbox_to_goal(session, composite_goal, record.task_id)
                    return current
                if composite_goal is not None:
                    self._link_result_outbox_to_goal(session, composite_goal, record.task_id)
                return current
            session.execute(
                text(
                    """INSERT INTO statistical_result_interpretation_outbox
                    (task_id, conversation_id, project_id, run_id, plan_revision_id,
                     result_id, result_bundle_hash, result_version, artifact_ids_json,
                     status, lease_owner, lease_expires_at, fencing_token, attempt_count,
                     retry_generation, next_attempt_at, last_error_code, continuation_message_id)
                    VALUES (:task_id, :conversation_id, :project_id, :run_id, :plan_revision_id,
                     :result_id, :bundle_hash, :result_version, :artifact_ids, 'ready',
                     NULL, NULL, 0, 0, 0, NULL, NULL, NULL)"""
                ),
                {
                    "task_id": record.task_id,
                    "conversation_id": record.conversation_id,
                    "project_id": record.project_id,
                    "run_id": record.run_id,
                    "plan_revision_id": record.plan_revision_id,
                    "result_id": record.result_id,
                    "bundle_hash": record.result_bundle_hash,
                    "result_version": record.result_version,
                    "artifact_ids": json.dumps(record.artifact_ids),
                },
            )
            row = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": record.task_id},
                )
                .mappings()
                .one()
            )
            if composite_goal is not None:
                self._link_result_outbox_to_goal(session, composite_goal, record.task_id)
            return self._result_interpretation_from_row(row)

    def _validate_composite_result_binding(
        self, session: Session, record: StatisticalResultInterpretationOutbox
    ) -> CompositeGoalIdentity | None:
        try:
            task = self._task_from_session(session, record.task_id)
        except NotFoundError:
            # Legacy repository-level outbox records may be used without a
            # WorkTask in isolated stores; they cannot be a composite child.
            return None
        refs = task.authority_refs
        attempt = (
            session.execute(
                text(
                    """SELECT goal.* FROM composite_goal_analysis_attempts AS attempt
                    JOIN composite_goal_links AS goal
                      ON goal.intent_task_id=attempt.intent_task_id
                    WHERE attempt.work_task_id=:task_id"""
                ),
                {"task_id": record.task_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            if refs.analysis_intent_task_id is not None or refs.analysis_request_hash is not None:
                raise ConflictError(
                    "composite_goal_result_attempt_missing",
                    "声明复合 intent 的统计任务没有对应的持久分析尝试关联。",
                )
            return None
        goal = self._composite_goal_from_row(attempt)
        conversation = (
            session.execute(
                text("SELECT project_id FROM conversations WHERE conversation_id=:conversation_id"),
                {"conversation_id": record.conversation_id},
            )
            .mappings()
            .first()
        )
        plan = (
            session.execute(
                text("SELECT project_id FROM plan_revisions WHERE plan_revision_id=:plan_id"),
                {"plan_id": record.plan_revision_id},
            )
            .mappings()
            .first()
        )
        run = (
            session.execute(
                text(
                    "SELECT project_id, plan_revision_id FROM workflow_runs WHERE run_id=:run_id"
                ),
                {"run_id": record.run_id},
            )
            .mappings()
            .first()
        )
        proposal_outbox = (
            session.execute(
                text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                {"intent_id": goal.intent_task_id},
            )
            .mappings()
            .first()
        )
        if (
            goal.analysis_work_task_id != record.task_id
            or goal.conversation_id != record.conversation_id
            or goal.project_id != record.project_id
            or goal.analysis_request_hash != refs.analysis_request_hash
            or refs.analysis_intent_task_id != goal.intent_task_id
            or refs.conversation_id != goal.conversation_id
            or refs.plan_revision_id != record.plan_revision_id
            or refs.workflow_run_id != record.run_id
            or task.status.value != "completed"
            or conversation is None
            or conversation["project_id"] != goal.project_id
            or plan is None
            or plan["project_id"] != goal.project_id
            or run is None
            or run["project_id"] != goal.project_id
            or run["plan_revision_id"] != record.plan_revision_id
            or proposal_outbox is None
            or not self._outbox_matches_goal(proposal_outbox, goal)
            or goal.result_interpretation_outbox_task_id not in {None, record.task_id}
        ):
            raise ConflictError(
                "composite_goal_result_binding_mismatch",
                "统计结果解释 outbox 未绑定当前目标的会话、项目、活动尝试、Plan 或 Run。",
            )
        return goal

    @staticmethod
    def _link_result_outbox_to_goal(
        session: Session, goal: CompositeGoalIdentity, task_id: str
    ) -> None:
        changed = cast(
            CursorResult[Any],
            session.execute(
                text(
                    "UPDATE composite_goal_links "
                    "SET result_interpretation_outbox_task_id=:task_id "
                    "WHERE intent_task_id=:intent_id "
                    "AND result_interpretation_outbox_task_id IS NULL"
                ),
                {"task_id": task_id, "intent_id": goal.intent_task_id},
            ),
        ).rowcount
        if changed == 0:
            current = session.execute(
                text(
                    "SELECT result_interpretation_outbox_task_id FROM composite_goal_links "
                    "WHERE intent_task_id=:intent_id"
                ),
                {"intent_id": goal.intent_task_id},
            ).scalar_one_or_none()
            if current != task_id:
                raise ConflictError(
                    "composite_goal_result_outbox_conflict",
                    "复合目标已绑定另一条统计结果解释 outbox。",
                )

    def get_result_interpretation_outbox(
        self, task_id: str
    ) -> StatisticalResultInterpretationOutbox:
        with self._read_session() as session:
            row = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("统计结果解释续接", task_id)
            return self._result_interpretation_from_row(row)

    def claim_result_interpretation_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None:
        owner = worker_id.strip()
        if not owner or len(owner) > 200 or lease_seconds < 1:
            raise ValueError("worker_id and positive lease_seconds are required")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            row = (
                session.execute(
                    text(
                        """SELECT task_id FROM statistical_result_interpretation_outbox
                    WHERE status='ready'
                      AND (lease_expires_at IS NULL OR lease_expires_at<=:now)
                      AND (next_attempt_at IS NULL OR next_attempt_at<=:now)
                    ORDER BY task_id LIMIT 1"""
                    ),
                    {"now": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            task_id = str(row["task_id"])
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE statistical_result_interpretation_outbox
                        SET lease_owner=:owner, lease_expires_at=:expires,
                            fencing_token=fencing_token+1, next_attempt_at=NULL
                        WHERE task_id=:task_id AND status='ready'
                          AND (lease_expires_at IS NULL OR lease_expires_at<=:now)
                          AND (next_attempt_at IS NULL OR next_attempt_at<=:now)"""
                    ),
                    {
                        "owner": owner,
                        "expires": now + lease_seconds,
                        "task_id": task_id,
                        "now": now,
                    },
                ),
            ).rowcount
            if changed != 1:
                return None
            fence = int(
                session.execute(
                    text(
                        "SELECT fencing_token FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": task_id},
                ).scalar_one()
            )
        return ResultInterpretationClaim(
            task_id=task_id,
            worker_id=owner,
            lease_expires_at=now + lease_seconds,
            fencing_token=fence,
        )

    def renew_result_interpretation_claim(
        self, claim: ResultInterpretationClaim, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        expires = now + lease_seconds
        with self.unit_of_work.atomic(), self._write_session() as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE statistical_result_interpretation_outbox
                        SET lease_expires_at=:expires
                        WHERE task_id=:task_id AND status='ready'
                          AND lease_owner=:owner AND fencing_token=:fence
                          AND lease_expires_at>:now"""
                    ),
                    {
                        "expires": expires,
                        "task_id": claim.task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                    },
                ),
            ).rowcount
            if changed != 1:
                return None
        return claim.model_copy(update={"lease_expires_at": expires})

    def defer_result_interpretation_claim(
        self,
        claim: ResultInterpretationClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool:
        if retry_after_seconds < 1 or not error_code or len(error_code) > 64:
            raise ValueError("positive retry delay and bounded error code are required")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE statistical_result_interpretation_outbox
                        SET attempt_count=attempt_count+1, last_error_code=:error_code,
                            next_attempt_at=:retry_at, lease_owner=NULL, lease_expires_at=NULL
                        WHERE task_id=:task_id AND status='ready'
                          AND lease_owner=:owner AND fencing_token=:fence
                          AND lease_expires_at>:now"""
                    ),
                    {
                        "error_code": error_code,
                        "retry_at": now + retry_after_seconds,
                        "task_id": claim.task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                    },
                ),
            ).rowcount
            return changed == 1

    def complete_result_interpretation_outbox(
        self,
        claim: ResultInterpretationClaim,
        *,
        status: ResultInterpretationStatus,
        continuation_message_id: str,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> StatisticalResultInterpretationOutbox:
        if status is ResultInterpretationStatus.READY or not continuation_message_id:
            raise ValueError("outbox completion requires a terminal status and assistant message")
        now = time.time()
        with self.unit_of_work.atomic(), self._write_session() as session:
            composite_row = (
                session.execute(
                    text(
                        """SELECT goal.* FROM composite_goal_analysis_attempts AS attempt
                        JOIN composite_goal_links AS goal
                          ON goal.intent_task_id=attempt.intent_task_id
                        WHERE attempt.work_task_id=:task_id"""
                    ),
                    {"task_id": claim.task_id},
                )
                .mappings()
                .first()
            )
            if composite_row is not None and status is ResultInterpretationStatus.COMPLETED:
                goal = self._composite_goal_from_row(composite_row)
                result_row = (
                    session.execute(
                        text(
                            "SELECT * FROM statistical_result_interpretation_outbox "
                            "WHERE task_id=:task_id"
                        ),
                        {"task_id": claim.task_id},
                    )
                    .mappings()
                    .one()
                )
                task = self._task_from_session(session, claim.task_id)
                attempt = (
                    session.execute(
                        text(
                            "SELECT proposal_hash FROM composite_goal_analysis_attempts "
                            "WHERE intent_task_id=:intent_id AND work_task_id=:task_id"
                        ),
                        {"intent_id": goal.intent_task_id, "task_id": claim.task_id},
                    )
                    .mappings()
                    .first()
                )
                message = (
                    session.execute(
                        text(
                            "SELECT conversation_id, role, payload_json FROM conversation_messages "
                            "WHERE message_id=:message_id"
                        ),
                        {"message_id": continuation_message_id},
                    )
                    .mappings()
                    .first()
                )
                try:
                    payload = json.loads(message["payload_json"]) if message is not None else {}
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                completion = (
                    payload.get("statistical_result_interpretation")
                    if isinstance(payload, dict)
                    else None
                )
                envelope = (
                    payload.get("analysis_intent_envelope") if isinstance(payload, dict) else None
                )
                step_hashes = tuple(
                    step.input_version for step in task.steps if step.kind == "statistical_run"
                )
                if (
                    goal.analysis_work_task_id != claim.task_id
                    or goal.result_interpretation_outbox_task_id != claim.task_id
                    or result_row["conversation_id"] != goal.conversation_id
                    or result_row["project_id"] != goal.project_id
                    or result_row["run_id"] != task.authority_refs.workflow_run_id
                    or result_row["plan_revision_id"] != task.authority_refs.plan_revision_id
                    or task.authority_refs.conversation_id != goal.conversation_id
                    or task.authority_refs.analysis_intent_task_id != goal.intent_task_id
                    or task.authority_refs.analysis_request_hash != goal.analysis_request_hash
                    or attempt is None
                    or step_hashes != (attempt["proposal_hash"],)
                    or message is None
                    or message["conversation_id"] != goal.conversation_id
                    or message["role"] != "assistant"
                    or not isinstance(completion, dict)
                    or completion.get("task_id") != claim.task_id
                    or completion.get("status") != ResultInterpretationStatus.COMPLETED.value
                    or completion.get("result_id") != result_row["result_id"]
                    or completion.get("result_version") != result_row["result_version"]
                    or not isinstance(envelope, dict)
                    or envelope.get("status") != "completed"
                    or envelope.get("task_id") != goal.intent_task_id
                    or envelope.get("conversation_id") != goal.conversation_id
                    or envelope.get("project_id") != goal.project_id
                    or envelope.get("analysis_request_hash") != goal.analysis_request_hash
                ):
                    raise ConflictError(
                        "composite_goal_result_continuation_mismatch",
                        "统计结果完成 continuation 未精确绑定 active task、结果和复合目标。",
                    )
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        """UPDATE statistical_result_interpretation_outbox
                        SET status=:status, continuation_message_id=:message_id,
                            attempt_count=attempt_count+:record_attempt,
                            last_error_code=:error_code, next_attempt_at=NULL,
                            lease_owner=NULL, lease_expires_at=NULL
                        WHERE task_id=:task_id AND status='ready'
                          AND lease_owner=:owner AND fencing_token=:fence
                          AND lease_expires_at>:now AND continuation_message_id IS NULL"""
                    ),
                    {
                        "status": status.value,
                        "message_id": continuation_message_id,
                        "record_attempt": int(record_attempt),
                        "error_code": error_code,
                        "task_id": claim.task_id,
                        "owner": claim.worker_id,
                        "fence": claim.fencing_token,
                        "now": now,
                    },
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError(
                    "result_interpretation_claim_lost",
                    "统计结果解释续接 claim 已过期或已被替换。本轮草稿未提交。",
                )
            row = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": claim.task_id},
                )
                .mappings()
                .one()
            )
            return self._result_interpretation_from_row(row)

    def requeue_result_interpretation_outbox(
        self, task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> StatisticalResultInterpretationOutbox | None:
        if not recoverable_error_codes:
            raise ValueError("manual requeue requires recoverable error codes")
        with self.unit_of_work.atomic(), self._write_session() as session:
            row = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": task_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            current = self._result_interpretation_from_row(row)
            if current.status is ResultInterpretationStatus.READY and current.retry_generation > 0:
                return current
            if (
                current.status is not ResultInterpretationStatus.WAITING_USER
                or current.last_error_code not in recoverable_error_codes
            ):
                return None
            binds = {f"error_{index}": value for index, value in enumerate(recoverable_error_codes)}
            placeholders = ", ".join(f":error_{index}" for index in range(len(binds)))
            changed = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        f"""UPDATE statistical_result_interpretation_outbox
                        SET status='ready', attempt_count=0, retry_generation=retry_generation+1,
                            fencing_token=fencing_token+1, next_attempt_at=NULL,
                            lease_owner=NULL, lease_expires_at=NULL,
                            continuation_message_id=NULL, last_error_code=NULL
                        WHERE task_id=:task_id AND status='waiting_user'
                          AND last_error_code IN ({placeholders})"""
                    ),
                    {"task_id": task_id, **binds},
                ),
            ).rowcount
            if changed != 1:
                return None
            updated = (
                session.execute(
                    text(
                        "SELECT * FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": task_id},
                )
                .mappings()
                .one()
            )
            return self._result_interpretation_from_row(updated)

    @staticmethod
    def _result_interpretation_from_row(row: Any) -> StatisticalResultInterpretationOutbox:
        return StatisticalResultInterpretationOutbox(
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            project_id=row["project_id"],
            run_id=row["run_id"],
            plan_revision_id=row["plan_revision_id"],
            result_id=row["result_id"],
            result_bundle_hash=row["result_bundle_hash"],
            result_version=row["result_version"],
            artifact_ids=tuple(json.loads(row["artifact_ids_json"])),
            status=row["status"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            fencing_token=row["fencing_token"],
            attempt_count=row["attempt_count"],
            retry_generation=row["retry_generation"],
            next_attempt_at=row["next_attempt_at"],
            last_error_code=row["last_error_code"],
            continuation_message_id=row["continuation_message_id"],
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
            self._sync_preprocessing_composite_goal(session, task)
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
